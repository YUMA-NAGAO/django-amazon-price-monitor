import logging

from . import app_settings as settings
from .api import get_api
from amazon.api import (
    AmazonProduct,
    AsinNotFound,
    LookupException,
)
from celery.task import PeriodicTask
from django.core.mail import send_mail
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import ugettext as _
from datetime import timedelta
from price_monitor.models import (
    Price,
    Product,
    Subscription,
)


logger = logging.getLogger('price_monitor')


class ProductSynchronizeTask(PeriodicTask):
    """
    Synchronizes Products in status "Created" (0) initially with Product API.
    """
    run_every = timedelta(minutes=5)

    def run(self, **kwargs):
        """
        Runs the synchronization by fetching settings.AMAZON_PRODUCT_SYNCHRONIZE_COUNT number of products and requests their data from Amazon.
        """
        products = self.get_products_to_sync()

        # exit if there is no food
        if len(products) == 0:
            logger.info('No products to sync.')
            return
        else:
            logger.info('Syncing %(count)d products.' % {'count': len(products)})

        try:
            lookup = get_api().lookup(ItemId=','.join(products.keys()))
        except (LookupException, AsinNotFound):
            # if the lookup for all ASINs fails, do one by one to get the erroneous one(s)
            for asin, product in products.items():
                try:
                    lookup = get_api().lookup(ItemId=asin)
                except (LookupException, AsinNotFound):
                    logger.exception('unable to lookup product with asin %s' % asin)
                    product.set_failed_to_sync()
                else:
                    self.sync_product(lookup, product)
        except UnicodeEncodeError:
            logger.exception('Unable to communicate with Amazon, the access key is probably not allowed to fetch Product API.')
        else:
            # api.lookup hides a list of AmazonProducts or a single AmazonProduct
            if type(lookup) == AmazonProduct:
                lookup = [lookup]

            # iterate an sync
            for amazon_product in lookup:
                self.sync_product(amazon_product, products[amazon_product.asin])

    def get_products_to_sync(self):
        """
        Returns the products to synchronize.
        These are newly created products with status "0" or products that are older than settings.AMAZON_PRODUCT_REFRESH_THRESHOLD_MINUTES.
        :return: dictionary with the Products
        :rtype: dict
        """
        # prefer already synced products over newly created
        products = {
            p.asin: p for p in Product.objects.select_related().filter(
                date_last_synced__lte=(timezone.now() - timedelta(minutes=settings.AMAZON_PRODUCT_REFRESH_THRESHOLD_MINUTES))
            )
        }

        if len(products) < settings.AMAZON_PRODUCT_SYNCHRONIZE_COUNT:
            # there is still some space for products to sync, append newly created if available
            products = dict(
                products.items() + {
                    p.asin: p for p in Product.objects.select_related().filter(status=0)
                        .order_by('date_creation')[:(settings.AMAZON_PRODUCT_SYNCHRONIZE_COUNT - len(products))]
                }.items()
            )

        return products

    def sync_product(self, amazon_product, product):
        """
        Synchronizes the given price_monitor.model.Product with the Amazon lookup product.
        :param amazon_product: the Amazon product
        :type amazon_product: amazon.api.AmazonProduct
        :param product: the product to update
        :type product: price_monitor.models.Product
        """
        now = timezone.now()

        product.title = amazon_product.title
        product.large_image_url = amazon_product.large_image_url
        product.medium_image_url = amazon_product.medium_image_url
        product.small_image_url = amazon_product.small_image_url
        product.tiny_image_url = amazon_product.tiny_image_url
        product.offer_url = amazon_product.offer_url
        product.status = 1
        product.date_last_synced = now
        product.save()

        # tuple: (price, currency)
        price = amazon_product.price_and_currency

        if not price[0] is None:
            # get all subscriptions of product that are subscribed to the current price or a higher one and
            # whose owners have not been notified about that particular subscription price since before settings.SUBSCRIPTION_RENOTIFICATION_MINUTES.
            for sub in Subscription.objects.filter(
                Q(
                    product=product,
                    price_limit__gte=price[0],
                    date_last_notification__lte=(timezone.now() - timedelta(minutes=settings.SUBSCRIPTION_RENOTIFICATION_MINUTES))
                ) | Q(
                    product=product,
                    price_limit__gte=price[0],
                    date_last_notification__isnull=True
                )
            ):
                # TODO use celery for this
                self.notify_subscriber(product, price[0], price[1], sub)

            # create the price entry
            Price.objects.create(
                value=price[0],
                currency=price[1],
                date_seen=now,
                product=product,
            )

    # TODO: celery-ize
    def notify_subscriber(self, product, price, currency, subscription):
        """
        Sends an email to the subscriber.
        :param product: the product to notify about
        :param price: the current price of the product
        :param currency:  the currency of the price
        :param subscription: the Subscription class connecting subscriber and product
        :type product: .Product
        :type price: float
        :type currency: string
        :type subscription: .Subscription
        """
        # TODO: move mail texts to settings
        send_mail(
            _('price limit for %(product)s reached' % {'product': product.title}),
            _(
                'The price limit of %(price_limit)0.2f %(currency)s has been reached for the article "%(product_title)s" - the current price is %(price)0.2f '
                '%(currency)s.\n\nPlease support our platform by using this link for buying: %(link)s\n\n\nRegards,\nThe Team' % {
                    'price_limit': subscription.price_limit,
                    'currency': currency,
                    'price': price,
                    'product_title': product.title,
                    'link': product.offer_url,
                }
            ),
            settings.PRICE_MONITOR_EMAIL_SENDER,
            [subscription.email_notification.email],
            fail_silently=False,
        )