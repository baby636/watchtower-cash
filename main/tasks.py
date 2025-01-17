import logging, json, requests
from urllib.parse import urlparse
from watchtower.settings import MAX_RESTB_RETRIES
from bitcash.transaction import calc_txid
from celery import shared_task
from main.models import (
    BlockHeight, 
    Token, 
    Transaction,
    Recipient,
    Subscription,
    Address,
    Wallet,
    WalletHistory,
    WalletNftToken
)
from celery.exceptions import MaxRetriesExceededError 
from main.utils.wallet import HistoryParser
from django.db.utils import IntegrityError
from django.conf import settings
from django.utils import timezone, dateparse
from django.db import transaction as trans
from celery import Celery
from main.utils.chunk import chunks
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
from main.utils.queries.bchd import BCHDQuery
from PIL import Image, ImageFile
from io import BytesIO 
import pytz
from celery import chord

LOGGER = logging.getLogger(__name__)
REDIS_STORAGE = settings.REDISKV


# NOTIFICATIONS
@shared_task(rate_limit='20/s', queue='send_telegram_message')
def send_telegram_message(message, chat_id):
    data = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }

    url = 'https://api.telegram.org/bot'
    response = requests.post(
        f"{url}{settings.TELEGRAM_BOT_TOKEN}/sendMessage", data=data
    )
    return f"send notification to {chat_id}"
  

@shared_task(bind=True, queue='client_acknowledgement', max_retries=3)
def client_acknowledgement(self, txid):
    this_transaction = Transaction.objects.filter(id=txid)
    third_parties = []
    if this_transaction.exists():
        transaction = this_transaction.first()
        block = None
        if transaction.blockheight:
            block = transaction.blockheight.number
        
        address = transaction.address

            
        subscriptions = Subscription.objects.filter(
            address=address             
        )

        if subscriptions.exists():
            
            for subscription in subscriptions:

                recipient = subscription.recipient
                websocket = subscription.websocket

                wallet_version = 1
                if address.wallet:
                    wallet_version = address.wallet.version
                else:
                    # Hardcoded date-based check for addresses that are not associated with wallets
                    v2_rollout_date_str = dateparse.parse_datetime('2021-09-11 00:00:00')
                    v2_rollout_date = pytz.UTC.localize(v2_rollout_date_str)
                    if address.date_created >= v2_rollout_date:
                        wallet_version = 2

                if wallet_version == 2:
                    data = {
                        'token_name': transaction.token.name,
                        'token_id':  'slp/' + transaction.token.tokenid if  transaction.token.tokenid  else 'bch',
                        'token_symbol': transaction.token.token_ticker.lower(),
                        'amount': transaction.amount,
                        'address': transaction.address.address,
                        'source': 'WatchTower',
                        'txid': transaction.txid,
                        'block': block,
                        'index': transaction.index,
                        'address_path' : transaction.address.address_path
                    }
                elif wallet_version == 1:
                    data = {
                        'amount': transaction.amount,
                        'address': transaction.address.address,
                        'source': 'WatchTower',
                        'token': transaction.token.tokenid or transaction.token.token_ticker.lower(),
                        'txid': transaction.txid,
                        'block': block,
                        'index': transaction.index,
                        'address_path' : transaction.address.address_path
                    }

                if recipient:
                    if recipient.valid:
                        if recipient.web_url:
                            LOGGER.info(f"Webhook call to be sent to: {recipient.web_url}")
                            LOGGER.info(f"Data: {str(data)}")
                            resp = requests.post(recipient.web_url,data=data)
                            if resp.status_code == 200:
                                this_transaction.update(acknowledged=True)
                                LOGGER.info(f'ACKNOWLEDGEMENT SENT TX INFO : {transaction.txid} TO: {recipient.web_url} DATA: {str(data)}')
                            elif resp.status_code == 404 or resp.status_code == 522 or resp.status_code == 502:
                                Recipient.objects.filter(id=recipient.id).update(valid=False)
                                LOGGER.info(f"!!! ATTENTION !!! THIS IS AN INVALID DESTINATION URL: {recipient.web_url}")
                            else:
                                LOGGER.error(resp)
                                self.retry(countdown=3)

                        if recipient.telegram_id:

                            if transaction.token.name != 'bch':
                                message=f"""<b>WatchTower Notification</b> ℹ️
                                    \n Address: {transaction.address.address}
                                    \n Token: {transaction.token.name}
                                    \n Token ID: {transaction.token.tokenid}
                                    \n Amount: {transaction.amount}
                                    \nhttps://explorer.bitcoin.com/bch/tx/{transaction.txid}
                                """
                            else:
                                message=f"""<b>WatchTower Notification</b> ℹ️
                                    \n Address: {transaction.address.address}
                                    \n Amount: {transaction.amount} BCH
                                    \nhttps://explorer.bitcoin.com/bch/tx/{transaction.txid}
                                """

                            args = ('telegram' , message, recipient.telegram_id)
                            third_parties.append(args)
                            this_transaction.update(acknowledged=True)

                if websocket:
                    
                    tokenid = ''
                    room_name = transaction.address.address.replace(':','_')
                    room_name += f'_{tokenid}'
                    channel_layer = get_channel_layer()
                    async_to_sync(channel_layer.group_send)(
                        f"{room_name}", 
                        {
                            "type": "send_update",
                            "data": data
                        }
                    )
                    if transaction.address.address.startswith('simpleledger:'):
                        tokenid = transaction.token.tokenid
                        room_name += f'_{tokenid}'
                        channel_layer = get_channel_layer()
                        async_to_sync(channel_layer.group_send)(
                            f"{room_name}", 
                            {
                                "type": "send_update",
                                "data": data
                            }
                        )
    return third_parties


@shared_task(queue='save_record')
def save_record(token, transaction_address, transactionid, amount, source, blockheightid=None, index=0, new_subscription=False, spent_txids=[]):
    """
        token                : can be tokenid (slp token) or token name (bch)
        transaction_address  : the destination address where token had been deposited.
        transactionid        : transaction id generated over blockchain.
        amount               : the amount being transacted.
        source               : the layer that summoned this function (e.g SLPDB, Bitsocket, BitDB, SLPFountainhead etc.)
        blockheight          : an optional argument indicating the block height number of a transaction.
        index          : used to make sure that each record is unique based on slp/bch address in a given transaction_id
    """
    subscription = Subscription.objects.filter(
        address__address=transaction_address             
    )
    
    spent_txids_check = Transaction.objects.filter(txid__in=spent_txids)

    # We are only tracking outputs of either subscribed addresses or those of transactions
    # that spend previous transactions with outputs involving tracked addresses
    if not subscription.exists() and not spent_txids_check.exists():
        return None, None

    address_obj, _ = Address.objects.get_or_create(address=transaction_address)

    try:
        index = int(index)
    except TypeError as exc:
        index = 0

    with trans.atomic():
        
        transaction_created = False

        if token.lower() == 'bch':
            token_obj, _ = Token.objects.get_or_create(name=token)
        else:
            token_obj, created = Token.objects.get_or_create(tokenid=token)
            if created:
                get_token_meta_data.delay(token)

        # try:

        #     #  USE FILTER AND BULK CREATE AS A REPLACEMENT FOR GET_OR_CREATE        
        #     tr = Transaction.objects.filter(
        #         txid=transactionid,
        #         address=address_obj,
        #         index=index,
        #     )
            
        #     if not tr.exists():

        #         transaction_data = {
        #             'txid': transactionid,
        #             'address': address_obj,
        #             'token': token_obj,
        #             'amount': amount,
        #             'index': index,
        #             'source': source
        #         }
        #         transaction_list = [Transaction(**transaction_data)]
        #         Transaction.objects.bulk_create(transaction_list)
        #         transaction_created = True
        # except IntegrityError:
        #     return None, None

        try:
            transaction_obj, transaction_created = Transaction.objects.get_or_create(
                txid=transactionid,
                address=address_obj,
                token=token_obj,
                amount=amount,
                index=index
            )

            if transaction_obj.source != source:
                transaction_obj.source = source

        except IntegrityError as exc:
            LOGGER.error('ERROR in saving txid: ' + transactionid)
            LOGGER.error(str(exc))
            return None, None

        if blockheightid is not None:
            transaction_obj.blockheight_id = blockheightid
            if new_subscription:
                transaction_obj.acknowledged = True

            # Automatically update all transactions with block height.
            Transaction.objects.filter(txid=transactionid).update(blockheight_id=blockheightid)

        # Check if address belongs to a wallet
        if address_obj.wallet:
            transaction_obj.wallet = address_obj.wallet

        # Save updates and trigger post-save signals
        transaction_obj.save()
        
        return transaction_obj.id, transaction_created


@shared_task(queue='bchdquery_transaction')
def bchdquery_transaction(txid, block_id, alert=True):
    source ='bchd'
    index = 0
    bchd = BCHDQuery()
    transaction = bchd._get_raw_transaction(txid)

    for output in transaction.outputs:
        if output.address:
            # save bch transaction
            amount = output.value / (10 ** 8)
            obj_id, created = save_record(
                'bch',
                'bitcoincash:%s' % output.address,
                txid,
                amount,
                source,
                blockheightid=block_id,
                index=index
            )
            if created and alert:
                third_parties = client_acknowledgement(obj_id)
                for platform in third_parties:
                    if 'telegram' in platform:
                        message = platform[1]
                        chat_id = platform[2]
                        send_telegram_message(message, chat_id)

        if output.slp_token.token_id:
            token_id = bytearray(output.slp_token.token_id).hex()
            amount = output.slp_token.amount / (10 ** output.slp_token.decimals)
            # save slp transaction
            obj_id, created = save_record(
                token_id,
                'simpleledger:%s' % output.slp_token.address,
                txid,
                amount,
                source,
                blockheightid=block_id,
                index=index
            )            
            if created and alert:
                third_parties = client_acknowledgement(obj_id)
                for platform in third_parties:
                    if 'telegram' in platform:
                        message = platform[1]
                        chat_id = platform[2]
                        send_telegram_message(message, chat_id)

        index += 1


@shared_task(bind=True, queue='manage_blocks')
def ready_to_accept(self, block_number, txs_count):
    BlockHeight.objects.filter(number=block_number).update(
        processed=True,
        transactions_count=txs_count,
        updated_datetime=timezone.now()
    )
    REDIS_STORAGE.set('READY', 1)
    REDIS_STORAGE.set('ACTIVE-BLOCK', '')
    return 'OK'


@shared_task(bind=True, queue='manage_blocks')
def manage_blocks(self):
    if b'READY' not in REDIS_STORAGE.keys(): REDIS_STORAGE.set('READY', 1)
    if b'ACTIVE-BLOCK' not in REDIS_STORAGE.keys(): REDIS_STORAGE.set('ACTIVE-BLOCK', '')
    if b'PENDING-BLOCKS' not in REDIS_STORAGE.keys(): REDIS_STORAGE.set('PENDING-BLOCKS', json.dumps([]))
    
    pending_blocks = REDIS_STORAGE.get('PENDING-BLOCKS').decode()
    blocks = json.loads(pending_blocks)
    blocks.sort()

    if len(blocks) == 0:
        unscanned_blocks = BlockHeight.objects.filter(
            processed=False,
            requires_full_scan=True
        ).values_list('number', flat=True)
        REDIS_STORAGE.set('PENDING-BLOCKS', json.dumps(list(unscanned_blocks)))


    if int(REDIS_STORAGE.get('READY').decode()): LOGGER.info('READY TO PROCESS ANOTHER BLOCK')
    if not blocks: return 'NO PENDING BLOCKS'
    
    discard_block = False
    if int(REDIS_STORAGE.get('READY').decode()):
        try:
            active_block = blocks[0]
            block = BlockHeight.objects.get(number=active_block)
        except BlockHeight.DoesNotExist:
            discard_block = True   
    
        if active_block in blocks:
            blocks.remove(active_block)
            blocks = list(set(blocks))  # Uniquify the list
            blocks.sort()  # Then sort, ascending
            pending_blocks = json.dumps(blocks)
            REDIS_STORAGE.set('PENDING-BLOCKS', pending_blocks)

        if not discard_block:
            REDIS_STORAGE.set('ACTIVE-BLOCK', active_block)
            REDIS_STORAGE.set('READY', 0)
            bchd = BCHDQuery()
            transactions = bchd.get_block(block.number, full_transactions=False)
            subtasks = []
            for tr in transactions:
                txid = bytearray(tr.transaction_hash[::-1]).hex()
                subtasks.append(bchdquery_transaction.si(txid, block.id))
            callback = ready_to_accept.si(block.number, len(subtasks))
            # Execute the workflow
            if subtasks:
                workflow = chord(subtasks)(callback)
                workflow.apply_async()

    active_block = str(REDIS_STORAGE.get('ACTIVE-BLOCK').decode())
    if active_block: return f'CURRENTLY PROCESSING BLOCK {str(active_block)}.'


@shared_task(bind=True, queue='get_latest_block')
def get_latest_block(self):
    # This task is intended to check new blockheight every 5 seconds through BCHD @ fountainhead.cash
    LOGGER.info('CHECKING THE LATEST BLOCK')
    bchd = BCHDQuery()
    number = bchd.get_latest_block()
    obj, created = BlockHeight.objects.get_or_create(number=number)
    if created: return f'*** NEW BLOCK { obj.number } ***'


@shared_task(bind=True, queue='get_utxos', max_retries=10)
def get_bch_utxos(self, address):
    try:
        obj = BCHDQuery()
        outputs = obj.get_utxos(address)
        source = 'bchd-query'
        saved_utxo_ids = []
        for output in outputs:
            hash = output.outpoint.hash 
            index = output.outpoint.index
            block = output.block_height
            tx_hash = bytearray(hash[::-1]).hex()
            amount = output.value / (10 ** 8)
            
            block, created = BlockHeight.objects.get_or_create(number=block)
            transaction_obj = Transaction.objects.filter(
                txid=tx_hash,
                address__address=address,
                amount=amount,
                index=index
            )
            if not transaction_obj.exists():
                args = (
                    'bch',
                    address,
                    tx_hash,
                    amount,
                    source,
                    block.id,
                    index,
                    True
                )
                txn_id, created = save_record(*args)
                transaction_obj = Transaction.objects.filter(id=txn_id)

                if created:
                    if not block.requires_full_scan:
                        qs = BlockHeight.objects.filter(id=block.id)
                        count = qs.first().transactions.count()
                        qs.update(processed=True, transactions_count=count)
            
            if transaction_obj.exists():
                # Mark as unspent, just in case it's already marked spent
                transaction_obj.update(spent=False)
                for obj in transaction_obj:
                    saved_utxo_ids.append(obj.id)

        # Mark other transactions of the same address as spent
        txn_check = Transaction.objects.filter(
            address__address=address,
            spent=False
        ).exclude(
            id__in=saved_utxo_ids
        ).update(
            spent=True
        )

    except Exception as exc:
        try:
            LOGGER.error(exc)
            self.retry(countdown=4)
        except MaxRetriesExceededError:
            LOGGER.error(f"CAN'T EXTRACT UTXOs OF {address} THIS TIME. THIS NEEDS PROPER FALLBACK.")


@shared_task(bind=True, queue='get_utxos', max_retries=10)
def get_slp_utxos(self, address):
    try:
        obj = BCHDQuery()
        outputs = obj.get_utxos(address)
        source = 'bchd-query'
        saved_utxo_ids = []
        for output in outputs:
            if output.slp_token.token_id:
                hash = output.outpoint.hash 
                tx_hash = bytearray(hash[::-1]).hex()
                index = output.outpoint.index
                token_id = bytearray(output.slp_token.token_id).hex() 
                amount = output.slp_token.amount / (10 ** output.slp_token.decimals)
                block = output.block_height
                
                block, _ = BlockHeight.objects.get_or_create(number=block)

                token_obj, _ = Token.objects.get_or_create(tokenid=token_id)

                transaction_obj = Transaction.objects.filter(
                    txid=tx_hash,
                    address__address=address,
                    token=token_obj,
                    amount=amount,
                    index=index
                )
                if not transaction_obj.exists():
                    args = (
                        token_id,
                        address,
                        tx_hash,
                        amount,
                        source,
                        block.id,
                        index,
                        True
                    )
                    txn_id, created = save_record(*args)
                    transaction_obj = Transaction.objects.filter(id=txn_id)
                    
                    if created:
                        if not block.requires_full_scan:
                            qs = BlockHeight.objects.filter(id=block.id)
                            count = qs.first().transactions.count()
                            qs.update(processed=True, transactions_count=count)
                
                if transaction_obj.exists():
                    # Mark as unspent, just in case it's already marked spent
                    transaction_obj.update(spent=False)
                    for obj in transaction_obj:
                        saved_utxo_ids.append(obj.id)
        
        # Mark other transactions of the same address as spent
        txn_check = Transaction.objects.filter(
            address__address=address,
            spent=False
        ).exclude(
            id__in=saved_utxo_ids
        ).update(
            spent=True
        )

    except Exception as exc:
        try:
            LOGGER.error(exc)
            self.retry(countdown=4)
        except MaxRetriesExceededError:
            LOGGER.error(f"CAN'T EXTRACT UTXOs OF {address} THIS TIME. THIS NEEDS PROPER FALLBACK.")


def is_url(url):
  try:
    result = urlparse(url)
    return all([result.scheme, result.netloc])
  except ValueError:
    return False


def download_image(token_id, url, resize=False):
    ImageFile.LOAD_TRUNCATED_IMAGES = True
    resp = requests.get(url, stream=True, timeout=300)
    image_file_name = None
    if resp.status_code == 200:
        content_type = resp.headers.get('content-type')
        if content_type.split('/')[0] == 'image':
            file_ext = content_type.split('/')[1]

            if resize:
                LOGGER.info('Saving resized images...')
                # Save medium size
                img = Image.open(BytesIO(resp.content))
                width, height = img.size
                medium_width = 450
                medium_height = medium_width * (height / width)
                medium_size = (medium_width, int(medium_height))
                medium_img = img.resize(medium_size, Image.ANTIALIAS)
                out_path = f"{settings.TOKEN_IMAGES_DIR}/{token_id}_medium.{file_ext}"
                medium_img.save(out_path, quality=95)
                LOGGER.info(out_path)

                # Save thumbnail size
                thumbnail_width = 150
                thumbnail_height = thumbnail_width * (height / width)
                thumbnail_size = (thumbnail_width, int(thumbnail_height))
                thumbnail_img = img.resize(thumbnail_size, Image.ANTIALIAS)
                out_path = f"{settings.TOKEN_IMAGES_DIR}/{token_id}_thumbnail.{file_ext}"
                thumbnail_img.save(out_path, quality=95)
                LOGGER.info(out_path)

            # Save original size
            LOGGER.info('Saving original image...')
            out_path = f"{settings.TOKEN_IMAGES_DIR}/{token_id}.{file_ext}"
            LOGGER.info(out_path)
            img.save(out_path, quality=95)
            image_file_name = f"{token_id}.{file_ext}"

    return resp.status_code, image_file_name


@shared_task(bind=True, queue='token_metadata', max_retries=3)
def get_token_meta_data(self, token_id):
    token_obj = Token.objects.get(
        tokenid=token_id
    )
    try:
        LOGGER.info('Fetching token metadata from BCHD...')
        bchd = BCHDQuery()
        txn = bchd.get_transaction(token_obj.tokenid, parse_slp=True)
        if txn:
            # Update token info
            info = txn['token_info']
            token_obj.name = info['name']
            token_obj.token_ticker = info['ticker']
            token_obj.token_type = info['type']
            token_obj.decimals = info['decimals']
            token_obj.date_updated = timezone.now()

            group = None
            if info['nft_token_group']:
                group, _ = Token.objects.get_or_create(tokenid=info['nft_token_group'])
                token_obj.nft_token_group = group

            token_obj.save()

            # Get image / logo URL
            image_file_name = None
            image_url = None
            status_code = 0

            if token_obj.token_type == 1:

                # Check slp-token-icons repo
                url = f"https://raw.githubusercontent.com/kosinusbch/slp-token-icons/master/128/{token_id}.png"
                status_code, image_file_name = download_image(token_id, url)

            if token_obj.token_type == 65:

                # Check if NFT group/parent token has image_base_url
                if group:
                    if 'image_base_url' in group.nft_token_group_details.keys():
                        image_base_url = group.nft_token_group_details['image_base_url']
                        image_type = group.nft_token_group_details['image_type']
                        url = f"{image_base_url}/{token_id}.{image_type}"
                        status_code, image_file_name = download_image(token_id, url, resize=True)
                else:
                    # TODO: Get group token metadata
                    pass

                if not image_file_name:
                    # Try getting image directly from document URL
                    url = info['document_url']
                    if is_url(url):
                        status_code, image_file_name = download_image(token_id, url, resize=True)

                # Disable simpleledger.info, it's being deprecated
                # if not image_file_name:
                #     # Scrape NFT image link from simpleledger.info
                #     url = 'http://simpleledger.info/token/' + token_id
                #     resp = requests.get(url)
                #     if resp.status_code == 200:
                #         soup = BeautifulSoup(resp.text, 'html')
                #         img_div = soup.find_all('div', {'class': 'token-icon-large'})
                #         if img_div:
                #             url = img_div[0].img.attrs['src']
                #             status_code, image_file_name = download_image(token_id, url, resize=True)

            if status_code == 200:
                if image_file_name:
                    image_server_base = 'https://images.watchtower.cash'
                    image_url = f"{image_server_base}/{image_file_name}"
                    if token_obj.token_type == 1:
                        Token.objects.filter(tokenid=token_id).update(
                            original_image_url=image_url,
                            date_updated=timezone.now()
                        )
                    if token_obj.token_type == 65:
                        Token.objects.filter(tokenid=token_id).update(
                            original_image_url=image_url,
                            medium_image_url=image_url.replace(token_id + '.', token_id + '_medium.'),
                            thumbnail_image_url=image_url.replace(token_id + '.', token_id + '_thumbnail.'),
                            date_updated=timezone.now()
                        )

            info_id = ''
            if token_obj.token_type:
                info_id = 'slp/' + token_id

            return {
                'id': info_id,
                'name': token_obj.name,
                'symbol': token_obj.token_ticker,
                'token_type': token_obj.token_type,
                'image_url': image_url or ''
            }

    except Exception as exc:
        LOGGER.error(str(exc))
        self.retry(countdown=5)


@shared_task(bind=True, queue='broadcast', max_retries=3)
def broadcast_transaction(self, transaction):
    txid = calc_txid(transaction)
    LOGGER.info(f'Broadcasting {txid}:  {transaction}')
    txn_check = Transaction.objects.filter(txid=txid)
    success = False
    if txn_check.exists():
        success = True
        return success, txid
    else:
        try:
            obj = BCHDQuery()
            try:
                txid = obj.broadcast_transaction(transaction)
                if txid:
                    if tx_found:
                        success = True
                        return success, txid
                    else:
                        self.retry(countdown=1)
                else:
                    self.retry(countdown=1)
            except Exception as exc:
                error = exc.details()
                LOGGER.error(error)
                return False, error
        except AttributeError:
            self.retry(countdown=1)


@shared_task(bind=True, queue='post_save_record')
def parse_wallet_history(self, txid, wallet_handle, tx_fee=None, senders=[], recipients=[]):
    wallet_hash = wallet_handle.split('|')[1]
    parser = HistoryParser(txid, wallet_hash)
    record_type, amount, change_address = parser.parse()
    wallet = Wallet.objects.get(wallet_hash=wallet_hash)
    if wallet.wallet_type == 'bch':
        txns = Transaction.objects.filter(
            txid=txid,
            address__address__startswith='bitcoincash:'
        )
    elif wallet.wallet_type == 'slp':
        txns = Transaction.objects.filter(
            txid=txid,
            address__address__startswith='simpleledger:'
        )
    txn = txns.last()
    if txn:
        if change_address:
            recipients = [(x, y) for x, y in recipients if x != change_address]

        history_check = WalletHistory.objects.filter(
            wallet=wallet,
            txid=txid
        )
        if history_check.exists():
            history_check.update(
                record_type=record_type,
                amount=amount,
                token=txn.token
            )
            if tx_fee and senders and recipients:
                history_check.update(
                    tx_fee=tx_fee,
                    senders=senders,
                    recipients=recipients
                )
        else:
            history = WalletHistory(
                wallet=wallet,
                txid=txid,
                record_type=record_type,
                amount=amount,
                token=txn.token,
                tx_fee=tx_fee,
                senders=senders,
                recipients=recipients,
                date_created=txn.date_created
            )
            history.save()

            if txn.token.token_type == 65:
                if record_type == 'incoming':
                    wallet_nft_token, created = WalletNftToken.objects.get_or_create(
                        wallet=wallet,
                        token=txn.token,
                        acquisition_transaction=txn
                    )
                    if created:
                        wallet_nft_token.acquisition_transaction = txn
                        wallet_nft_token.save()
                elif record_type == 'outgoing':
                    wallet_nft_token_check = WalletNftToken.objects.filter(
                        wallet=wallet,
                        token=txn.token,
                        date_dispensed__isnull=True
                    )
                    if wallet_nft_token_check.exists():
                        wallet_nft_token = wallet_nft_token_check.last()
                        wallet_nft_token.date_dispensed = txn.date_created
                        wallet_nft_token.dispensation_transaction = txn
                        wallet_nft_token.save()


@shared_task(bind=True, queue='post_save_record', max_retries=10)
def transaction_post_save_task(self, address, transaction_id, blockheight_id=None):
    transaction = Transaction.objects.get(id=transaction_id)
    txid = transaction.txid
    blockheight = None
    if blockheight_id:
        blockheight = BlockHeight.objects.get(id=blockheight_id)

    wallets = []
    txn_address = Address.objects.get(address=address)
    if txn_address.wallet:
        wallets.append(txn_address.wallet.wallet_type + '|' + txn_address.wallet.wallet_hash)

    slp_tx = None
    bch_tx = None

    # Extract tx_fee, senders, and recipients
    tx_fee = 0
    senders = {
        'bch': [],
        'slp': []
    }
    recipients = {
        'bch': [],
        'slp': []
    }

    bchd = BCHDQuery()
    parse_slp = address.startswith('simpleledger')

    if parse_slp:
        # Parse SLP senders and recipients
        slp_tx = bchd.get_transaction(txid, parse_slp=True)
        if not slp_tx:
            self.retry(countdown=5)
            return
        tx_fee = slp_tx['tx_fee']
        if slp_tx['valid']:
            if txn_address.wallet:
                if txn_address.wallet.wallet_type == 'slp':
                    for tx_input in slp_tx['inputs']:
                        if 'amount' in tx_input.keys():
                            senders['slp'].append(
                                (
                                    tx_input['address'],
                                    tx_input['amount']
                                ) 
                            )
                if 'outputs' in slp_tx.keys():
                    recipients['slp'] = [(i['address'], i['amount']) for i in slp_tx['outputs']]

    # Parse BCH senders and recipients
    bch_tx = bchd.get_transaction(txid)
    if not bch_tx:
        self.retry(countdown=5)
        return
    tx_fee = bch_tx['tx_fee']
    if txn_address.wallet:
        if txn_address.wallet.wallet_type == 'bch':
            senders['bch'] = [(i['address'], i['value']) for i in bch_tx['inputs']]
            if 'outputs' in bch_tx.keys():
                recipients['bch'] = [(i['address'], i['value']) for i in bch_tx['outputs']]

    if parse_slp:
        if slp_tx is None:
            slp_tx = bchd.get_transaction(txid, parse_slp=True)
        spent_txids = []

        # Mark SLP tx inputs as spent
        for tx_input in slp_tx['inputs']:
            try:
                address = Address.objects.get(address=tx_input['address'])
                if address.wallet:
                    wallets.append('slp|' + address.wallet.wallet_hash)
            except Address.DoesNotExist:
                pass
            spent_txids.append(tx_input['txid'])
            txn_check = Transaction.objects.filter(
                txid=tx_input['txid'],
                index=tx_input['spent_index']
            )
            txn_check.update(
                spent=True,
                spending_txid=txid
            )

            if txn_check.exists():
                txn_obj = txn_check.last()
                if txn_obj.token.token_type == 65:
                    wallet_nft_token = WalletNftToken.objects.get(
                        acquisition_transaction=txn_obj
                    )
                    wallet_nft_token.date_dispensed = timezone.now()
                    wallet_nft_token.dispensation_transaction = transaction
                    wallet_nft_token.save()

        # Parse SLP tx outputs
        if slp_tx['valid']:
            for tx_output in slp_tx['outputs']:
                try:
                    address = Address.objects.get(address=tx_output['address'])
                    if address.wallet:
                        wallets.append('slp|' + address.wallet.wallet_hash)
                except Address.DoesNotExist:
                    pass
                txn_check = Transaction.objects.filter(
                    txid=slp_tx['txid'],
                    address__address=tx_output['address'],
                    index=tx_output['index']
                )
                if not txn_check.exists():
                    blockheight_id = None
                    if blockheight:
                        blockheight_id = blockheight.id
                    args = (
                        slp_tx['token_id'],
                        tx_output['address'],
                        slp_tx['txid'],
                        tx_output['amount'],
                        'bchd-query',
                        blockheight_id,
                        tx_output['index']
                    )
                    obj_id, created = save_record(*args, spent_txids=spent_txids)
                    if created:
                        third_parties = client_acknowledgement(obj_id)
                        for platform in third_parties:
                            if 'telegram' in platform:
                                message = platform[1]
                                chat_id = platform[2]
                                send_telegram_message(message, chat_id)

    # Parse BCH inputs
    if bch_tx is None:
        bch_tx = bchd.get_transaction(txid)

    spent_txids = []
    
    # Mark BCH tx inputs as spent
    for tx_input in bch_tx['inputs']:
        try:
            address = Address.objects.get(address=tx_input['address'])
            if address.wallet:
                wallets.append('bch|' + address.wallet.wallet_hash)
        except Address.DoesNotExist:
            pass

        spent_txids.append(tx_input['txid'])
        txn_check = Transaction.objects.filter(
            txid=tx_input['txid'],
            index=tx_input['spent_index']
        )
        txn_check.update(
            spent=True,
            spending_txid=txid
        )

    # Parse BCH tx outputs
    for tx_output in bch_tx['outputs']:
        try:
            address = Address.objects.get(address=tx_output['address'])
            if address.wallet:
                wallets.append('bch|' + address.wallet.wallet_hash)
        except Address.DoesNotExist:
            pass
        txn_check = Transaction.objects.filter(
            txid=bch_tx['txid'],
            address__address=tx_output['address'],
            index=tx_output['index']
        )
        if not txn_check.exists():
            blockheight_id = None
            if blockheight:
                blockheight_id = blockheight.id
            value = tx_output['value'] / 10 ** 8
            args = (
                'bch',
                tx_output['address'],
                bch_tx['txid'],
                value,
                'bchd-query',
                blockheight_id,
                tx_output['index']
            )
            obj_id, created = save_record(*args, spent_txids=spent_txids)
            if created:
                third_parties = client_acknowledgement(obj_id)
                for platform in third_parties:
                    if 'telegram' in platform:
                        message = platform[1]
                        chat_id = platform[2]
                        send_telegram_message(message, chat_id)

    # Call task to parse wallet history
    for wallet_handle in set(wallets):
        if wallet_handle.split('|')[0] == 'slp':
            if senders['slp'] and recipients['slp']:
                parse_wallet_history.delay(
                    txid,
                    wallet_handle,
                    tx_fee,
                    senders['slp'],
                    recipients['slp']
                )
        if wallet_handle.split('|')[0] == 'bch':
            if senders['bch'] and recipients['bch']:
                parse_wallet_history.delay(
                    txid,
                    wallet_handle,
                    tx_fee,
                    senders['bch'],
                    recipients['bch']
                )


@shared_task(queue='celery_rebuild_history')
def rebuild_wallet_history(wallet_hash):
    addresses = Address.objects.filter(wallet__wallet_hash=wallet_hash)
    for address in addresses:
        for transaction in address.transactions.all():
            history_check = WalletHistory.objects.filter(
                txid=transaction.txid,
                recipients__contains=[address.address]
            )
            if not history_check.exists():
                transaction.save()

            if transaction.token.token_type == 65:
                wallet_nft_token_check = WalletNftToken.objects.filter(
                    wallet__wallet_hash=wallet_hash,
                    token=transaction.token,
                    acquisition_transaction=transaction
                )
                if wallet_nft_token_check.exists():
                    wallet_nft_token = wallet_nft_token_check.last()
                else:
                    wallet_nft_token = WalletNftToken(
                        wallet=Wallet.objects.get(wallet_hash=wallet_hash),
                        token=transaction.token,
                        date_acquired=transaction.date_created,
                        acquisition_transaction=transaction
                    )
                    wallet_nft_token.save()

                if  wallet_nft_token.date_dispensed is None and transaction.spent:
                    try:
                        spending_tx = Transaction.objects.get(
                            txid=transaction.spending_txid,
                            token=transaction.token
                        )
                        wallet_nft_token.date_dispensed = spending_tx.date_created
                        wallet_nft_token.dispensation_transaction = spending_tx
                        wallet_nft_token.save()
                    except Transaction.DoesNotExist:
                        pass


@shared_task(queue='rescan_utxos')
def rescan_utxos(wallet_hash):
    wallet = Wallet.object.get(wallet_hash=wallet_hash)
    addresses = wallet.addresses.filter(transactions__spent=False)
    for address in addresses:
        if wallet.wallet_type == 'bch':
            get_bch_utxos(address.address)
        elif wallet.wallet_type == 'slp':
            get_slp_utxos(address.address)
