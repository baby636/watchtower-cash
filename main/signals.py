from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver
from rest_framework.authtoken.models import Token
from main.models import BlockHeight
from django.utils import timezone

@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def create_auth_token(sender, instance=None, created=False, **kwargs):
    if created:
        Token.objects.create(user=instance)

@receiver(post_save, sender=BlockHeight)
def blockheight_post_save(sender, instance=None, created=False, **kwargs):
    if not created:
        all_transactions  = len(instance.genesis) + instance.transactions.distinct('txid').count() + len(instance.problematic)
        if all_transactions == instance.transactions_count:
            BlockHeight.objects.filter(id=instance.id).update(processed=True, updated_datetime=timezone.now())