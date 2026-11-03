"""Moneta API privata e motore movimenti ricorrenti.

API Gateway autentica JWT; la Lambda richiede un access token del client previsto.
Il job schedulato è invocabile soltanto da EventBridge Scheduler via IAM.
"""
import base64
import calendar
import datetime as dt
import json
import logging
import os
import re
import time
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

import boto3
from botocore.exceptions import ClientError

LOG = logging.getLogger(__name__)
LOG.setLevel(logging.WARNING)
TABLE = boto3.resource('dynamodb').Table(os.environ['TABLE_NAME'])
CLIENT_ID = os.environ['COGNITO_CLIENT_ID']
ID_RE = re.compile(r'^[A-Za-z0-9_-]{1,80}$')
DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
EXPENSE = frozenset(('Casa', 'Affitto e mutuo', 'Condominio', 'Alimentari', 'Ristoranti', 'Bar e caffè', 'Bollette', 'Telefonia e Internet', 'Abbonamenti', 'Trasporti', 'Carburante', 'Auto e manutenzione', 'Assicurazioni', 'Salute', 'Farmacia', 'Istruzione', 'Animali domestici', 'Abbigliamento', 'Shopping', 'Tecnologia', 'Svago', 'Calcetto', 'Viaggi', 'Regali', 'Tasse', 'Spese bancarie', 'Altro'))
INCOME = frozenset(('Stipendio', 'Bonus e premi', 'Straordinari', 'Lavoro autonomo', 'Affitti percepiti', 'Vendite', 'Regali ricevuti', 'Interessi', 'Dividendi', 'Rimborsi', 'Extra', 'Altre entrate'))
FREQUENCIES = frozenset(('weekly', 'monthly', 'quarterly', 'yearly'))

class BadRequest(Exception):
    pass

def json_serializer(obj):
    """Converte i numeri DynamoDB Decimal in numeri JSON."""
    if isinstance(obj, Decimal):
        if not obj.is_finite():
            raise TypeError('Decimal non finito non serializzabile')
        if obj == obj.to_integral_value():
            return int(obj)
        return float(obj)
    raise TypeError(f'Object of type {type(obj).__name__} is not JSON serializable')

def response(status, data=None):
    out = {'statusCode': status, 'headers': {'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store'}}
    if data is not None:
        out['body'] = json.dumps(data, ensure_ascii=False, separators=(',', ':'), default=json_serializer)
    return out

def body_json(event):
    raw = event.get('body') or ''
    if not isinstance(raw, str) or len(raw) > 32000:
        raise BadRequest('Richiesta troppo grande')
    try:
        if event.get('isBase64Encoded'):
            raw = base64.b64decode(raw, validate=True).decode('utf-8')
        obj = json.loads(raw)
    except (ValueError, UnicodeError):
        raise BadRequest('JSON non valido') from None
    if not isinstance(obj, dict):
        raise BadRequest('Oggetto JSON richiesto')
    return obj

def cents(value, allow_zero=False):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise BadRequest('Importo non valido')
    try:
        dec = Decimal(str(value))
    except InvalidOperation:
        raise BadRequest('Importo non valido') from None
    if not dec.is_finite() or dec > 1000000 or (dec < 0 if allow_zero else dec <= 0) or dec*100 != (dec*100).to_integral_value():
        raise BadRequest('Importo fuori limite o oltre due decimali')
    return int(dec*100)

def date_value(value):
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        raise BadRequest('Data non valida')
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise BadRequest('Data non valida') from None

def validate_record(obj, expected_id=None):
    ident = obj.get('id')
    if not isinstance(ident, str) or not ID_RE.fullmatch(ident) or (expected_id is not None and ident != expected_id):
        raise BadRequest('ID non valido')
    description = obj.get('description')
    if not isinstance(description, str) or not (1 <= len(description.strip()) <= 120):
        raise BadRequest('Descrizione non valida')
    kind = obj.get('type')
    if kind not in ('income', 'expense'):
        raise BadRequest('Tipo non valido')
    category = obj.get('category')
    if category not in (INCOME if kind == 'income' else EXPENSE):
        raise BadRequest('Categoria non valida')
    note = obj.get('note', '')
    if not isinstance(note, str) or len(note) > 300:
        raise BadRequest('Nota troppo lunga')
    # Solo i campi dichiarati vengono persistiti.
    return {'id': ident, 'description': description.strip(), 'type': kind,
            'category': category, 'date': date_value(obj.get('date')).isoformat(),
            'amountCents': cents(obj.get('amount')), 'note': note,
            'accountId': account_id(obj.get('accountId','bank'))}

def validate_rule(obj, expected_id=None):
    record = validate_record({**obj, 'date': obj.get('startDate')}, expected_id)
    frequency = obj.get('frequency')
    if frequency not in FREQUENCIES:
        raise BadRequest('Frequenza non valida')
    interval = obj.get('interval', 1)
    if type(interval) is not int or not 1 <= interval <= 12:
        raise BadRequest('Intervallo da 1 a 12')
    end_date = obj.get('endDate') or None
    if end_date and date_value(end_date) < date_value(record['date']):
        raise BadRequest('Scadenza precedente all’inizio')
    if end_date:
        end_date = date_value(end_date).isoformat()
    return {**{k:v for k,v in record.items() if k != 'date'}, 'startDate': record['date'],
            'frequency': frequency, 'interval': interval, 'endDate': end_date,
            'active': obj.get('active', True) is not False}

def public(row):
    val = dict(row)
    val['amount'] = int(val.pop('amountCents')) / 100
    # Le contribuzioni legacy (prima della 2.2) erano già trasferimenti,
    # ma non avevano un flag esplicito. Inferenza conservativa dalla struttura.
    if (val.get('type') == 'income' and val.get('paymentSource') == 'personal'
            and val.get('createdBy')):
        val['isTransfer'] = True
    if (val.get('linkedSpaceId') and val.get('sourceTransactionId')
            and val.get('type') == 'expense'
            and str(val.get('description','')).startswith('Versamento al fondo · ')):
        val['isTransfer'] = True
    return val

def query_user(pk):
    rows, last = [], None
    while True:
        args = {'KeyConditionExpression': 'PK = :pk', 'ExpressionAttributeValues': {':pk': pk}}
        if last:
            args['ExclusiveStartKey'] = last
        result = TABLE.query(**args)
        rows.extend(result.get('Items', []))
        if len(rows) > 10000:
            raise BadRequest('Archivio troppo grande: necessaria paginazione')
        last = result.get('LastEvaluatedKey')
        if not last:
            return rows

def month_offset(start, step):
    idx = start.year*12 + (start.month-1) + step
    year, month = divmod(idx, 12)
    month += 1
    return dt.date(year, month, min(start.day, calendar.monthrange(year,month)[1]))

def occurrence_at(rule, index):
    start = date_value(rule['startDate'])
    step = index*int(rule['interval'])
    freq = rule['frequency']
    if freq == 'weekly':
        return start + dt.timedelta(days=7*step)
    return month_offset(start, step*{'monthly':1, 'quarterly':3, 'yearly':12}[freq])

def due_dates(rule, today):
    """Fino a 24 occorrenze non ancora scadute, ancorate alla data iniziale.
    Ripetere l'esecuzione è sicuro: le put condizionali evitano duplicati.
    """
    end = date_value(rule['endDate']) if rule.get('endDate') else today
    limit = min(today, end)
    start = date_value(rule['startDate'])
    if limit < start or not rule.get('active', True):
        return []
    elapsed = (limit-start).days
    if rule['frequency'] == 'weekly':
        last_index = elapsed//(7*int(rule['interval']))
    else:
        span = (limit.year-start.year)*12 + limit.month-start.month
        width = int(rule['interval'])*{'monthly':1,'quarterly':3,'yearly':12}[rule['frequency']]
        last_index = span//width + 1
    result = []
    for i in range(max(0,last_index-23),last_index+2):
        due = occurrence_at(rule,i)
        if due <= limit:
            result.append(due)
    return result[-24:]

def run_recurring(today=None):
    """Job giornaliero condiviso. Un solo movimento per spazio e scadenza.

    La proiezione personale è nella stessa TransactWriteItems del movimento
    familiare. Le righe legacy personali continuano a funzionare inalterate.
    """
    today = today or dt.datetime.now(dt.timezone.utc).date()
    created, count, last = 0, 0, None
    while True:
        args = {'FilterExpression': 'begins_with(SK, :prefix)',
                'ExpressionAttributeValues':{':prefix':'REC#'}}
        if last:
            args['ExclusiveStartKey'] = last
        result = TABLE.scan(**args)
        for row in result.get('Items', []):
            count += 1
            if count > 1500:
                raise RuntimeError('Troppe ricorrenze: è necessario passare a un indice dedicato')
            rule = row['value']
            for due in due_dates(rule,today):
                txid = 'rec-'+rule['id'][:45]+'-'+due.strftime('%Y%m%d')
                rec = {k:rule[k] for k in ('description','type','category','note','amountCents')}
                rec['accountId']=rule.get('accountId','bank')
                rec.update({'id':txid,'date':due.isoformat(),'recurringId':rule['id']})
                try:
                    if row['PK'].startswith('SPACE#'):
                        sid = row['PK'][6:]
                        creator = rule.get('createdBy')
                        share_record(rec, rule.get('paymentSource','fund'))
                        rec['createdBy'] = creator or ''
                        if rec['paymentSource'] == 'personal' and (not creator or not get_row(row['PK'],'MEMBER#'+creator)):
                            LOG.warning('Ricorrenza %s: autore non più membro, pagamento personale sospeso', rule['id'])
                            continue
                        actions = [put_action({'PK':row['PK'],'SK':'TX#'+txid,'value':rec})]
                        if creator and get_row(row['PK'],'MEMBER#'+creator):
                            mirror = mirrored_transaction(sid,rec)
                            actions.append(put_action({'PK':user_key(creator),
                                                       'SK':'TX#'+mirror['id'],'value':mirror}))
                        txn(actions)
                    else:
                        checks=card_transaction_guard(row['PK'],rec)
                        if checks:
                            txn(checks+[put_action({'PK':row['PK'],'SK':'TX#'+txid,'value':rec})])
                        else:
                            TABLE.put_item(Item={'PK':row['PK'],'SK':'TX#'+txid,'value':rec},
                                           ConditionExpression='attribute_not_exists(PK)')
                    created += 1
                except ClientError as err:
                    code = err.response['Error']['Code']
                    if code == 'ConditionalCheckFailedException':
                        continue
                    if code == 'TransactionCanceledException':
                        # Transazione duplicata se Scheduler ritenta dopo un timeout.
                        if get_row(row['PK'],'TX#'+txid):
                            continue
                    raise
        last = result.get('LastEvaluatedKey')
        if not last:
            return {'created':created,'rules':count,'date':today.isoformat()}

# --- Moneta 2.0: accessi per spazio, inviti e contabilità collegata ---
import hashlib
import uuid
from boto3.dynamodb.types import TypeSerializer

SERIALIZER = TypeSerializer()
USER_POOL_ID = os.environ['USER_POOL_ID']
SPACES_LIMIT = 15
MEMBERS_LIMIT = 20


def user_key(sub):
    return 'USER#' + sub


def space_key(space_id):
    if not isinstance(space_id, str) or not re.fullmatch(r's-[0-9a-f]{32}', space_id):
        raise BadRequest('Identificativo del bilancio non valido')
    return 'SPACE#' + space_id


def groups_admin(claims):
    groups = claims.get('cognito:groups', [])
    if isinstance(groups, str):
        groups = [part.strip().strip('"\'') for part in groups.strip('[]').split(',')]
    return isinstance(groups, list) and 'Admins' in groups


def get_row(pk, sk):
    return TABLE.get_item(Key={'PK':pk,'SK':sk}, ConsistentRead=True).get('Item')


def list_prefix(pk, prefix):
    items, last = [], None
    while True:
        params = {'KeyConditionExpression':'PK = :pk AND begins_with(SK, :prefix)',
                  'ExpressionAttributeValues':{':pk':pk, ':prefix':prefix}}
        if last:
            params['ExclusiveStartKey'] = last
        data = TABLE.query(**params)
        items.extend(data.get('Items', []))
        if len(items) > 10000:
            raise BadRequest('Archivio troppo grande')
        last = data.get('LastEvaluatedKey')
        if not last:
            return items


def membership(space_id, sub, write=False, owner=False):
    sk = space_key(space_id)
    row = get_row(sk, 'MEMBER#'+sub)
    role = (row or {}).get('role')
    if role not in ('owner','editor','viewer') or (owner and role != 'owner') or (write and role == 'viewer'):
        raise PermissionError('Non hai accesso a questo bilancio o i permessi necessari')
    return role


def auth_check(space_id, sub, owner=False):
    return {'ConditionCheck':{'TableName':TABLE.name,
        'Key':wire({'PK':space_key(space_id),'SK':'MEMBER#'+sub}),
        'ConditionExpression':'#role = :expected' if owner else '#role IN (:owner, :editor)',
        'ExpressionAttributeNames':{'#role':'role'},
        'ExpressionAttributeValues':(
            {':expected':SERIALIZER.serialize('owner')} if owner else
            {':owner':SERIALIZER.serialize('owner'), ':editor':SERIALIZER.serialize('editor')})}}


def wire(item):
    return {k:SERIALIZER.serialize(v) for k,v in item.items()}


def txn(actions):
    # Un'unica tabella; la transazione è atomica, anche se contiene una proiezione personale.
    boto3.client('dynamodb').transact_write_items(TransactItems=actions)


def put_action(item, condition='attribute_not_exists(PK)', values=None):
    out = {'TableName':TABLE.name, 'Item':wire(item)}
    if condition:
        out['ConditionExpression'] = condition
    if values:
        out['ExpressionAttributeValues'] = wire(values)
    return {'Put':out}


def del_action(pk, sk, condition='attribute_exists(PK)', values=None):
    out = {'TableName':TABLE.name, 'Key':wire({'PK':pk,'SK':sk}), 'ConditionExpression':condition}
    if values:
        out['ExpressionAttributeValues'] = wire(values)
    return {'Delete':out}


def linked_id(space_id, transaction_id):
    digest = hashlib.sha256((space_id+'|'+transaction_id).encode('utf-8')).hexdigest()[:30]
    return 'link-'+digest


def mirrored_transaction(space_id, original):
    """Proiezione personale; incide sul saldo solo se si usano soldi personali.

    Un versamento personale al fondo diventa un'uscita personale e un'entrata comune.
    Le spese dal fondo restano visibili nel personale ma NON lo addebitano.
    """
    personal_funded = original.get('paymentSource') == 'personal'
    item = {k:original[k] for k in ('description','category','date','note','amountCents')}
    if personal_funded:
        item['accountId'] = original.get('accountId','bank')
    item['id'] = linked_id(space_id, original['id'])
    item['type'] = 'expense' if personal_funded else original['type']
    if personal_funded and original['type'] == 'income':
        item['description'] = 'Versamento al fondo · '+original['description']
        item['category'] = 'Casa'
    item.update({'linkedSpaceId':space_id, 'sourceTransactionId':original['id'],
                 'affectsBalance':personal_funded, 'paymentSource':original['paymentSource'],
                 'readOnly':True})
    return item


def shared_transaction(original, actor):
    # Solo campi noti; non è possibile fabbricare createdBy dalla richiesta.
    return {**original, 'createdBy':actor, 'paymentSource':'fund', 'affectsBalance':True}


def share_record(record, source):
    if source not in ('fund','personal'):
        raise BadRequest('Origine pagamento non valida')
    record['paymentSource'] = source
    # Un'uscita pagata direttamente da un membro non consuma il fondo familiare.
    record['affectsBalance'] = record['type'] == 'income' or source == 'fund'
    return record


def transaction_actions(space_id, actor, record, method):
    pk = space_key(space_id)
    sk = 'TX#'+record['id']
    previous = get_row(pk, sk)
    if method == 'create' and previous:
        raise BadRequest('ID movimento già esistente')
    if method == 'update' and (not previous or previous.get('skipped')):
        raise KeyError('Movimento non trovato')
    if method == 'update' and (previous.get('value') or {}).get('ledgerOperationId'):
        raise BadRequest('Operazione del fondo non modificabile; crea una rettifica tracciata')
    owner_id = (previous or {}).get('value', {}).get('createdBy', actor)
    if method == 'update' and (previous.get('value') or {}).get('recurringId'):
        record['recurringId'] = previous['value']['recurringId']
    record['createdBy'] = owner_id
    cond = 'attribute_not_exists(PK)' if method == 'create' else 'attribute_exists(PK)'
    items = [auth_check(space_id, actor), put_action({'PK':pk,'SK':sk,'value':record},cond)]
    mirrored = mirrored_transaction(space_id, record)
    # Se l'autore non è più membro, non si reintroducono dati nel suo spazio personale.
    if not get_row(pk, 'MEMBER#'+owner_id):
        if method == 'create':
            raise PermissionError('Autore non membro dello spazio')
    else:
        if owner_id != actor:
            items.append(auth_check(space_id,owner_id))
        mirror_row = {'PK':user_key(owner_id),'SK':'TX#'+mirrored['id'],'value':mirrored}
        # Le modifiche devono ripristinare un collegamento coerente anche in caso di dati legacy.
        items.append(put_action(mirror_row, 'attribute_not_exists(PK)' if method == 'create' else
                                    'attribute_not_exists(PK) OR #v.linkedSpaceId = :sid',
                                    None if method == 'create' else {':sid':space_id}))
        if method == 'update':
            items[-1]['Put']['ExpressionAttributeNames'] = {'#v':'value'}
    txn(items)
    return record


def delete_shared_transaction(space_id, sub, rid):
    pk = space_key(space_id)
    row = get_row(pk, 'TX#'+rid)
    if not row:
        raise KeyError('Movimento non trovato')
    if row.get('skipped'):
        return
    original = row.get('value') or {}
    if original.get('ledgerOperationId'):
        raise BadRequest('Operazione del fondo non eliminabile come movimento ordinario')
    actions = [auth_check(space_id,sub)]
    if original.get('recurringId'):
        actions.append(put_action({'PK':pk,'SK':'TX#'+rid,'skipped':True}, 'attribute_exists(PK)'))
    else:
        actions.append(del_action(pk,'TX#'+rid))
    creator = original.get('createdBy')
    if creator:
        mirror_sk = 'TX#'+linked_id(space_id, rid)
        mirror = get_row(user_key(creator),mirror_sk)
        if mirror:
            actions.append(del_action(user_key(creator),mirror_sk))
    txn(actions)


def new_space(sub, payload):
    title = payload.get('name')
    if not isinstance(title, str) or not 2 <= len(title.strip()) <= 60:
        raise BadRequest('Nome del bilancio: da 2 a 60 caratteri')
    if len(list_prefix(user_key(sub),'SPACE#')) >= SPACES_LIMIT:
        raise BadRequest('Numero massimo di bilanci raggiunto')
    sid = 's-'+uuid.uuid4().hex
    stamp = dt.datetime.now(dt.timezone.utc).isoformat()
    meta = {'id':sid, 'name':title.strip(), 'createdBy':sub, 'createdAt':stamp}
    txn([put_action({'PK':space_key(sid),'SK':'META','value':meta}),
         put_action({'PK':space_key(sid),'SK':'MEMBER#'+sub,'role':'owner','joinedAt':stamp}),
         put_action({'PK':user_key(sub),'SK':'SPACE#'+sid,'role':'owner','name':meta['name']})])
    return meta


def user_spaces(sub):
    return [{'id':r['SK'][6:], 'name':r['name'], 'role':r['role']}
            for r in list_prefix(user_key(sub),'SPACE#')]


def pending_invites(sub):
    now = int(time.time())
    return [r['value'] for r in list_prefix(user_key(sub),'INV#')
            if int(r['value'].get('expiresAt',0)) > now]


def invite_member(space_id, sub, payload):
    membership(space_id, sub, owner=True)
    email = payload.get('email','')
    role = payload.get('role', 'editor')
    if not isinstance(email,str) or not re.fullmatch(r'[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.-]{1,250}\.[A-Za-z]{2,20}', email) or role not in ('editor','viewer'):
        raise BadRequest('Email o ruolo non valido')
    if len(list_prefix(space_key(space_id), 'MEMBER#')) >= MEMBERS_LIMIT:
        raise BadRequest('Numero massimo di membri raggiunto')
    cognito = boto3.client('cognito-idp')
    result = cognito.list_users(UserPoolId=USER_POOL_ID, Filter='email = "'+email.lower()+'"', Limit=2)
    users = result.get('Users',[])
    target = None
    for user in users:
        attrs = {a['Name']:a['Value'] for a in user.get('Attributes', [])}
        if attrs.get('email','').lower() == email.lower() and attrs.get('email_verified') == 'true':
            target = attrs.get('sub')
    if not target or target == sub:
        raise BadRequest('Destinatario non trovato o indirizzo non verificato in Cognito')
    if len(list_prefix(user_key(target),'INV#')) >= 20:
        raise BadRequest('Il destinatario ha troppi inviti in sospeso')
    if get_row(space_key(space_id),'MEMBER#'+target):
        raise BadRequest('La persona partecipa già a questo bilancio')
    meta = get_row(space_key(space_id),'META')
    if not meta:
        raise BadRequest('Bilancio inesistente')
    iid = 'i-'+uuid.uuid4().hex
    invite = {'id':iid,'spaceId':space_id,'spaceName':meta['value']['name'],
              'role':role,'email':email.lower(),'invitedBy':sub,'expiresAt':int(time.time())+7*86400}
    txn([auth_check(space_id,sub,owner=True),
         put_action({'PK':user_key(target),'SK':'INV#'+iid,'value':invite}),
         put_action({'PK':space_key(space_id),'SK':'INV#'+iid,'target':target,'value':invite})])
    return {'sent':True, 'expiresAt':invite['expiresAt']}


def accept_invite(sub, invite_id, accept):
    if not re.fullmatch(r'i-[0-9a-f]{32}', invite_id or ''):
        raise BadRequest('Invito non valido')
    item = get_row(user_key(sub),'INV#'+invite_id)
    inv = (item or {}).get('value')
    if not inv or inv['expiresAt'] <= int(time.time()):
        raise KeyError('Invito inesistente o scaduto')
    sid = inv['spaceId']
    actions = [del_action(user_key(sub),'INV#'+invite_id),
               del_action(space_key(sid),'INV#'+invite_id)]
    if accept:
        if not get_row(space_key(sid),'META'):
            raise KeyError('Bilancio eliminato')
        stamp = dt.datetime.now(dt.timezone.utc).isoformat()
        actions.extend([put_action({'PK':space_key(sid),'SK':'MEMBER#'+sub,
                                    'role':inv['role'],'email':inv.get('email',''),'joinedAt':stamp}),
                        put_action({'PK':user_key(sub),'SK':'SPACE#'+sid,
                                    'role':inv['role'],'name':inv['spaceName']})])
    txn(actions)
    return {'accepted':accept,'spaceId':sid}


def remove_member(space_id, sub, target):
    membership(space_id,sub,owner=True)
    if target == sub:
        raise BadRequest('Il proprietario non può lasciare il proprio bilancio')
    if not isinstance(target,str) or not re.fullmatch(r'[0-9a-fA-F-]{16,64}',target):
        raise BadRequest('Identificativo membro non valido')
    member = get_row(space_key(space_id),'MEMBER#'+target)
    if not member:
        raise KeyError('Membro inesistente')
    if member.get('role') == 'owner':
        raise BadRequest('Non è possibile rimuovere il proprietario')
    txn([auth_check(space_id,sub,owner=True),
         del_action(space_key(space_id),'MEMBER#'+target,'#role <> :owner', {':owner':'owner'}),
         del_action(user_key(target),'SPACE#'+space_id)])
    return {'removed':True}


def list_members(space_id, sub):
    membership(space_id,sub)
    return [{'id':row['SK'][7:],'role':row['role'],'email':row.get('email','')}
            for row in list_prefix(space_key(space_id),'MEMBER#')]


# --- Moneta 2.1: preferiti privati e scadenze una tantum ---
FAVORITES_LIMIT = 40
PLANNED_LIMIT = 200


def validated_favorite(payload, expected_id=None):
    ident = payload.get('id')
    if not isinstance(ident, str) or not ID_RE.fullmatch(ident) or (expected_id and ident != expected_id):
        raise BadRequest('ID preferito non valido')
    space_id = payload.get('spaceId') or ''
    if space_id:
        space_key(space_id)
    kind = payload.get('type')
    if kind not in ('income', 'expense'):
        raise BadRequest('Tipo preferito non valido')
    category = payload.get('category')
    if category not in (INCOME if kind == 'income' else EXPENSE):
        raise BadRequest('Categoria preferito non valida')
    description = payload.get('description')
    if not isinstance(description, str) or not 1 <= len(description.strip()) <= 120:
        raise BadRequest('Descrizione preferito non valida')
    note = payload.get('note', '')
    if not isinstance(note, str) or len(note) > 300:
        raise BadRequest('Nota preferito non valida')
    source = payload.get('paymentSource', 'fund')
    if source not in ('fund', 'personal'):
        raise BadRequest('Pagamento preferito non valido')
    return {'id':ident,'spaceId':space_id,'type':kind,'category':category,
            'description':description.strip(),'amountCents':cents(payload.get('amount')),
            'paymentSource':source,'note':note,'accountId':account_id(payload.get('accountId','bank'))}


def favorites_for(sub, space_id):
    return [public(row['value']) for row in list_prefix(user_key(sub),'FAV#')
            if row['value'].get('spaceId','') == (space_id or '')]


def planned_record(payload, expected_id=None):
    if len(str(payload.get('id',''))) > 75:
        raise BadRequest('ID scadenza troppo lungo')
    # Riutilizza le validazioni dei movimenti per mantenere compatibili categorie e importi.
    data = validate_record({**payload,'date':payload.get('dueDate')},expected_id)
    source = payload.get('paymentSource','fund')
    if source not in ('fund','personal'):
        raise BadRequest('Origine pagamento non valida')
    return {**{key:value for key,value in data.items() if key!='date'},
            'dueDate':data['date'],'paymentSource':source,'status':'scheduled'}


def complete_planned(data_pk, space_id, sub, ident, paid_date):
    if not isinstance(ident,str) or not ID_RE.fullmatch(ident):
        raise BadRequest('ID scadenza non valido')
    original = get_row(data_pk,'PLAN#'+ident)
    if not original:
        raise KeyError('Scadenza inesistente')
    plan = original['value']
    if plan.get('status') == 'completed':
        return {'completed':True,'transactionId':plan.get('transactionId')}
    if plan.get('status') != 'scheduled':
        raise BadRequest('Stato scadenza non valido')
    txid = 'plan-'+ident
    if len(txid) > 80:
        raise BadRequest('ID scadenza troppo lungo')
    record = {key:plan[key] for key in ('description','type','category','amountCents','note')}
    record['accountId']=plan.get('accountId','bank')
    record.update({'id':txid,'date':date_value(paid_date).isoformat(),'plannedId':ident})
    actions = []
    if space_id:
        actions.append(auth_check(space_id,sub))
        share_record(record,plan.get('paymentSource','fund'))
        record['createdBy'] = sub
    actions.append(put_action({'PK':data_pk,'SK':'TX#'+txid,'value':record}))
    completed = {**plan,'status':'completed','transactionId':txid,
                 'completedAt':dt.datetime.now(dt.timezone.utc).isoformat(),'completedBy':sub}
    actions.append(put_action({'PK':data_pk,'SK':'PLAN#'+ident,'value':completed},
                              '#v.#status = :scheduled', {':scheduled':'scheduled'}))
    actions[-1]['Put']['ExpressionAttributeNames']={'#v':'value','#status':'status'}
    if space_id:
        mirror = mirrored_transaction(space_id,record)
        actions.append(put_action({'PK':user_key(sub),'SK':'TX#'+mirror['id'],'value':mirror}))
    if not space_id:
        account_for(data_pk,record.get('accountId','bank'),expense=record['type']=='expense')
        actions=card_transaction_guard(data_pk,record)+actions
    txn(actions)
    return {'completed':True,'transactionId':txid}


# --- Moneta 2.2: fondo avanzato, scritture immutabili e Insights personali ---
FUND_OPERATION_LIMIT = 5000
CAPITAL_ITEM_LIMIT = 120

def safe_id(value):
    if not isinstance(value, str) or not ID_RE.fullmatch(value) or len(value) > 72:
        raise BadRequest('Identificativo operazione non valido')
    return value


def positive_amount(value):
    return cents(value)


def ledger_transaction(txid, description, category, kind, amount, date, opid, **extra):
    return {'id':txid,'description':description,'type':kind,'category':category,
            'date':date,'amountCents':amount,'note':'',
            'ledgerOperationId':opid, **extra}


def read_fund(space_id, sub):
    membership(space_id, sub)
    pk = space_key(space_id)
    today=dt.datetime.now(ZoneInfo('Europe/Rome')).date().isoformat()
    tx_rows = [tx for tx in state_for(pk)['transactions'] if tx['date']<=today]
    balance_cents = sum(round(Decimal(str(t['amount']))*100)*(1 if t['type']=='income' else -1)
                        for t in tx_rows if t.get('affectsBalance',True))
    operations = [r['value'] for r in list_prefix(pk,'LEDGER#')]
    reversed_ids = {r['SK'][4:] for r in list_prefix(pk,'REV#')}
    advances = [r['value'] for r in list_prefix(pk,'ADV#')]
    contributions = {}
    for op in operations:
        if op['kind'] == 'contribution' and op['id'] not in reversed_ids:
            who = op['createdBy']
            contributions[who] = contributions.get(who,0)+int(op['amountCents'])
    return {'balance':balance_cents/100,
            'contributions':[{'userId':who,'amount':amount/100} for who,amount in sorted(contributions.items())],
            'operations':[public({**op,'isReversed':op['id'] in reversed_ids}) for op in sorted(operations,key=lambda r:(r['date'],r['id']),reverse=True)[:100]],
            'advances':[{'id':a['id'],'description':a['description'],'date':a['date'],
                         'payerId':a['payerId'],'amount':int(a['amountCents'])/100,
                         'settlementMode':a['settlementMode'],
                         'allocations':{key:int(val)/100 for key,val in a['allocations'].items()},
                         'outstanding':{key:int(val)/100 for key,val in a['outstanding'].items()},
                         'settled':not any(int(x)>0 for x in a['outstanding'].values()),
                         'isReversed':a['id'] in reversed_ids}
                        for a in sorted(advances,key=lambda r:r['date'],reverse=True)[:200]],
            'note':'Il saldo include anche i movimenti preesistenti del bilancio. Le attribuzioni storiche non vengono inventate.'}


def member_exists_check(space_id, member_id):
    return {'ConditionCheck':{'TableName':TABLE.name,
       'Key':wire({'PK':space_key(space_id),'SK':'MEMBER#'+member_id}),
       'ConditionExpression':'attribute_exists(PK)'}}


def validate_allocation(space_id, allocations, total):
    if not isinstance(allocations,dict) or not 1 <= len(allocations) <= MEMBERS_LIMIT:
        raise BadRequest('Inserisci le quote dei membri')
    parsed = {}
    for sub,amount in allocations.items():
        if not isinstance(sub,str) or not re.fullmatch(r'[A-Za-z0-9-]{10,80}',sub):
            raise BadRequest('ID membro non valido')
        if not get_row(space_key(space_id),'MEMBER#'+sub):
            raise BadRequest('La quota appartiene a un utente non membro')
        parsed[sub]=cents(amount,allow_zero=True)
    if sum(parsed.values()) != total:
        raise BadRequest('La somma delle quote deve coincidere con la spesa')
    return parsed


def create_fund_operation(space_id, actor, payload):
    membership(space_id,actor,write=True)
    pk=space_key(space_id)
    opid=safe_id(payload.get('id'))
    kind=payload.get('kind')
    if kind not in ('contribution','advance','reimbursement'):
        raise BadRequest('Operazione del fondo non valida')
    amount=positive_amount(payload.get('amount'))
    date=date_value(payload.get('date')).isoformat()
    if date > dt.datetime.now(ZoneInfo('Europe/Rome')).date().isoformat():
        raise BadRequest('Le operazioni contabili devono essere già avvenute')
    stamp=dt.datetime.now(dt.timezone.utc).isoformat()
    title=payload.get('description','')
    if not isinstance(title,str) or not 1 <= len(title.strip()) <= 120:
        raise BadRequest('Descrizione obbligatoria (massimo 120 caratteri)')
    title=title.strip()
    if len(list_prefix(pk,'LEDGER#')) >= FUND_OPERATION_LIMIT:
        raise BadRequest('Limite operazioni raggiunto')
    op={'id':opid,'kind':kind,'date':date,'description':title,'amountCents':amount,
        'createdBy':actor,'createdAt':stamp}
    actions=[auth_check(space_id,actor), put_action({'PK':pk,'SK':'LEDGER#'+opid,'value':op})]
    txid='fund-'+opid
    if len(txid)>80:
        raise BadRequest('ID troppo lungo (massimo 75 caratteri)')
    if kind=='contribution':
        family=ledger_transaction(txid,'Versamento · '+title,'Extra','income',amount,date,opid,
                                  createdBy=actor,paymentSource='personal',affectsBalance=True,
                                  isTransfer=True)
        personal=mirrored_transaction(space_id,family)
        personal.update({'isTransfer':True,'ledgerOperationId':opid,'readOnly':True})
        actions.extend([put_action({'PK':pk,'SK':'TX#'+txid,'value':family}),
                        put_action({'PK':user_key(actor),'SK':'TX#'+personal['id'],'value':personal})])
    elif kind=='advance':
        category=payload.get('category')
        if category not in EXPENSE:
            raise BadRequest('Categoria spesa non valida')
        allocations=validate_allocation(space_id,payload.get('allocations'),amount)
        mode=payload.get('settlementMode')
        if mode not in ('fund','direct','none'):
            raise BadRequest('Modalità rimborso non valida')
        outstanding = ({'fund':amount} if mode=='fund' else
                       {who:value for who,value in allocations.items() if who != actor and value > 0}
                       if mode=='direct' else {})
        claim={'id':opid,'description':title,'date':date,'payerId':actor,'amountCents':amount,
               'allocations':allocations,'outstanding':outstanding,'settlementMode':mode,
               'version':1,'createdAt':stamp}
        family=ledger_transaction(txid,title,category,'expense',amount,date,opid,
                                  createdBy=actor,paymentSource='personal',affectsBalance=False)
        personal=mirrored_transaction(space_id,family)
        personal['ledgerOperationId']=opid
        actions.extend([put_action({'PK':pk,'SK':'ADV#'+opid,'value':claim}),
                        put_action({'PK':pk,'SK':'TX#'+txid,'value':family}),
                        put_action({'PK':user_key(actor),'SK':'TX#'+personal['id'],'value':personal})])
        for member_id in allocations:
            if member_id != actor:
                actions.append(member_exists_check(space_id,member_id))
        op['settlementMode']=mode
    else:
        advance_id=safe_id(payload.get('advanceId'))
        row=get_row(pk,'ADV#'+advance_id)
        if not row:
            raise KeyError('Anticipo inesistente')
        claim=row['value']; mode=claim['settlementMode']
        if claim.get('reversedAt') or get_row(pk,'REV#'+advance_id):
            raise BadRequest('Anticipo già stornato')
        if date < claim['date']:
            raise BadRequest('Il rimborso non può precedere l’anticipo')
        payer=claim['payerId']
        if not get_row(pk,'MEMBER#'+payer):
            raise BadRequest('Il beneficiario non è più membro del fondo')
        debtor = 'fund' if mode=='fund' else payload.get('debtorId')
        if mode=='none' or debtor not in claim['outstanding']:
            raise BadRequest('Non ci sono rimborsi aperti per questo soggetto')
        remaining=int(claim['outstanding'][debtor])
        if remaining < amount:
            raise BadRequest('Il rimborso supera il credito residuo')
        if debtor!='fund':
            if not isinstance(debtor,str) or not get_row(pk,'MEMBER#'+debtor):
                raise BadRequest('Il debitore non è più membro')
            if actor != debtor and membership(space_id,actor) != 'owner':
                raise PermissionError('Solo il debitore o il proprietario può registrare il rimborso diretto')
        updated={**claim,'outstanding':{**claim['outstanding'],debtor:remaining-amount},
                 'version':int(claim['version'])+1}
        version_action=put_action({'PK':pk,'SK':'ADV#'+advance_id,'value':updated},
                                   '#v.#version = :previous',{':previous':int(claim['version'])})
        version_action['Put']['ExpressionAttributeNames']={'#v':'value','#version':'version'}
        actions.append(version_action)
        personal_income=ledger_transaction('settle-'+opid,'Rimborso · '+title,'Rimborsi','income',
                                           amount,date,opid,affectsBalance=True,isTransfer=True,
                                           readOnly=True,linkedSpaceId=space_id)
        actions.append(put_action({'PK':user_key(payer),'SK':'TX#'+personal_income['id'],
                                   'value':personal_income}))
        if debtor=='fund':
            shared_out=ledger_transaction('settle-'+opid,'Rimborso · '+title,'Casa','expense',
                                          amount,date,opid,affectsBalance=True,isTransfer=True,
                                          createdBy=actor,paymentSource='fund')
            actions.append(put_action({'PK':pk,'SK':'TX#'+shared_out['id'],'value':shared_out}))
        else:
            personal_out=ledger_transaction('settle-'+opid,'Rimborso a un membro · '+title,'Casa',
                                            'expense',amount,date,opid,affectsBalance=True,
                                            isTransfer=True,readOnly=True,linkedSpaceId=space_id)
            actions.append(put_action({'PK':user_key(debtor),'SK':'TX#'+personal_out['id'],
                                       'value':personal_out}))
            if debtor != actor:
                actions.append(member_exists_check(space_id,debtor))
        op.update({'advanceId':advance_id,'debtorId':debtor,'recipientId':payer})
        if payer != actor and payer != debtor:
            actions.append(member_exists_check(space_id,payer))
    # Corregge il record operazione con le informazioni di riconciliazione.
    actions[1]=put_action({'PK':pk,'SK':'LEDGER#'+opid,'value':op})
    txn(actions)
    return public(op)


def reverse_fund_operation(space_id, actor, operation_id, payload):
    """Compensazione tracciata, senza modificare o cancellare le scritture originali.

    Per ripristinare un rimborso parziale, la versione del credito è controllata
    nella stessa transazione. Le operazioni originali restano consultabili.
    """
    membership(space_id, actor, write=True)
    opid = safe_id(operation_id)
    pk = space_key(space_id)
    original_row = get_row(pk, 'LEDGER#'+opid)
    if not original_row:
        raise KeyError('Operazione non trovata')
    original = original_row['value']
    if original.get('kind') not in ('contribution','advance','reimbursement'):
        raise BadRequest('Questa operazione non può essere stornata')
    if actor != original['createdBy'] and membership(space_id,actor) != 'owner':
        raise PermissionError('Può stornare solo il creatore o il proprietario del fondo')
    if get_row(pk,'REV#'+opid):
        raise BadRequest('Operazione già stornata')
    reason=payload.get('reason')
    if not isinstance(reason,str) or not 8 <= len(reason.strip()) <= 250:
        raise BadRequest('Specifica il motivo della rettifica (8-250 caratteri)')
    today=dt.datetime.now(ZoneInfo('Europe/Rome')).date().isoformat()
    reversed_id='rev-'+opid
    amount=int(original['amountCents'])
    entry={'id':reversed_id,'kind':'reversal','reverses':opid,
           'originalKind':original['kind'],'date':today,'amountCents':amount,
           'description':'Storno · '+original['description'],'reason':reason.strip(),
           'createdBy':actor,'createdAt':dt.datetime.now(dt.timezone.utc).isoformat()}
    actions=[auth_check(space_id,actor),
             put_action({'PK':pk,'SK':'REV#'+opid,'reversalId':reversed_id}),
             put_action({'PK':pk,'SK':'LEDGER#'+reversed_id,'value':entry})]
    extra={'ledgerOperationId':reversed_id,'reversesOperationId':opid,
           'isCorrection':True,'isTransfer':True,'readOnly':True}
    creator=original['createdBy']
    if original['kind']=='contribution':
        if not get_row(pk,'MEMBER#'+creator):
            raise BadRequest('Autore del versamento non più membro: rettifica manuale necessaria')
        actions.extend([
            put_action({'PK':pk,'SK':'TX#rvf-'+opid,
                        'value':ledger_transaction('rvf-'+opid,entry['description'],'Casa',
                                  'expense',amount,today,reversed_id,affectsBalance=True,**extra_without_ledger(extra))}),
            put_action({'PK':user_key(creator),'SK':'TX#rvc-'+opid,
                        'value':ledger_transaction('rvc-'+opid,entry['description'],'Rimborsi',
                                  'income',amount,today,reversed_id,affectsBalance=True,
                                  linkedSpaceId=space_id,**extra_without_ledger(extra))})])
        if creator!=actor:
            actions.append(member_exists_check(space_id,creator))
    elif original['kind']=='advance':
        claimrow=get_row(pk,'ADV#'+opid)
        if not claimrow:
            raise BadRequest('Anticipo privo della scrittura di credito')
        claim=claimrow['value']
        originally=({ 'fund':amount } if claim['settlementMode']=='fund' else
                    {who:int(v) for who,v in claim['allocations'].items()
                     if who!=claim['payerId'] and int(v)>0}
                    if claim['settlementMode']=='direct' else {})
        if {k:int(v) for k,v in claim['outstanding'].items()}!=originally:
            raise BadRequest('Prima storna tutti i rimborsi collegati a questo anticipo')
        if not get_row(pk,'MEMBER#'+claim['payerId']):
            raise BadRequest('Anticipante non più membro: rettifica manuale necessaria')
        newclaim={**claim,'reversedAt':today,'version':int(claim['version'])+1}
        check=put_action({'PK':pk,'SK':'ADV#'+opid,'value':newclaim},
                         '#v.#version = :version',{':version':int(claim['version'])})
        check['Put']['ExpressionAttributeNames']={'#v':'value','#version':'version'}
        actions.append(check)
        # Uno storno di spesa anticipata non restituisce denaro al fondo.
        actions.extend([
          put_action({'PK':pk,'SK':'TX#rvf-'+opid,
                      'value':ledger_transaction('rvf-'+opid,entry['description'],
                            'Altre entrate','income',amount,today,reversed_id,
                            affectsBalance=False,**extra_without_ledger(extra))}),
          put_action({'PK':user_key(claim['payerId']),'SK':'TX#rvc-'+opid,
                      'value':ledger_transaction('rvc-'+opid,entry['description'],
                            'Rimborsi','income',amount,today,reversed_id,
                            affectsBalance=True,linkedSpaceId=space_id,**extra_without_ledger(extra))})])
        if claim['payerId'] != actor:
            actions.append(member_exists_check(space_id,claim['payerId']))
    else:
        advance_id=original['advanceId']
        claimrow=get_row(pk,'ADV#'+advance_id)
        if not claimrow or get_row(pk,'REV#'+advance_id):
            raise BadRequest('Anticipo associato mancante o stornato')
        claim=claimrow['value']
        debtor=original['debtorId']
        if debtor not in claim['outstanding']:
            raise BadRequest('Credito originale non compatibile')
        # Riporta in vita la quota già rimborsata, senza duplicare un pagamento.
        updated={**claim,'outstanding':{**claim['outstanding'],
                         debtor:int(claim['outstanding'][debtor])+amount},
                 'version':int(claim['version'])+1}
        maximum=(int(claim['amountCents']) if debtor=='fund' else int(claim['allocations'].get(debtor,0)))
        if updated['outstanding'][debtor]>maximum:
            raise BadRequest('Il credito risulterebbe superiore alla quota iniziale')
        check=put_action({'PK':pk,'SK':'ADV#'+advance_id,'value':updated},
                         '#v.#version = :version',{':version':int(claim['version'])})
        check['Put']['ExpressionAttributeNames']={'#v':'value','#version':'version'}
        actions.append(check)
        recipient=original['recipientId']
        if not get_row(pk,'MEMBER#'+recipient):
            raise BadRequest('Beneficiario non più membro: rettifica manuale necessaria')
        actions.append(put_action({'PK':user_key(recipient),'SK':'TX#rvc-'+opid,
                      'value':ledger_transaction('rvc-'+opid,entry['description'],
                          'Casa','expense',amount,today,reversed_id,affectsBalance=True,
                          linkedSpaceId=space_id,**extra_without_ledger(extra))}))
        if debtor=='fund':
            actions.append(put_action({'PK':pk,'SK':'TX#rvf-'+opid,
                         'value':ledger_transaction('rvf-'+opid,entry['description'],
                         'Altre entrate','income',amount,today,reversed_id,
                         affectsBalance=True,**extra_without_ledger(extra))}))
        else:
            if not get_row(pk,'MEMBER#'+debtor):
                raise BadRequest('Debitore non più membro: rettifica manuale necessaria')
            actions.append(put_action({'PK':user_key(debtor),'SK':'TX#rvd-'+opid,
                         'value':ledger_transaction('rvd-'+opid,entry['description'],
                         'Rimborsi','income',amount,today,reversed_id,
                         affectsBalance=True,linkedSpaceId=space_id,**extra_without_ledger(extra))}))
            if debtor!=actor:
                actions.append(member_exists_check(space_id,debtor))
        if recipient!=actor and recipient!=debtor:
            actions.append(member_exists_check(space_id,recipient))
    txn(actions)
    return {'reversed':True,'reversalId':reversed_id,'originalId':opid,'reason':reason.strip()}


def extra_without_ledger(extra):
    """ledger_transaction riceve l'id dell'operazione come parametro posizionale."""
    return {key:value for key,value in extra.items() if key!='ledgerOperationId'}


def positive_decimal(value, field, max_digits=8, allow_zero=False):
    try:
        if isinstance(value,bool) or not isinstance(value,(str,int,float)):
            raise ValueError()
        amount=Decimal(str(value))
        if not amount.is_finite() or amount<0 or (amount==0 and not allow_zero) or amount>1000000:
            raise ValueError()
        if amount.as_tuple().exponent < -max_digits:
            raise ValueError()
        return amount
    except (ValueError,InvalidOperation):
        raise BadRequest(field+' non valido') from None


def capital_item(payload, expected_id=None, revision=1):
    ident=safe_id(payload.get('id'))
    if expected_id and ident!=expected_id:
        raise BadRequest('Identificativo patrimonio non coincidente')
    kind=payload.get('kind')
    if kind not in ('investment','other','cash_outside_ledger','debt'):
        raise BadRequest('Tipo elemento patrimoniale non valido')
    name=payload.get('name')
    if not isinstance(name,str) or not 1 <= len(name.strip()) <= 100:
        raise BadRequest('Nome del capitale non valido')
    as_of=date_value(payload.get('asOf')).isoformat()
    if as_of>dt.datetime.now(ZoneInfo('Europe/Rome')).date().isoformat():
        raise BadRequest('Non puoi indicare una valutazione futura')
    asset={'id':ident,'kind':kind,'name':name.strip(),
           'amountCents':cents(payload.get('value'),allow_zero=True),
           'asOf':as_of,'revision':revision,
           'updatedAt':dt.datetime.now(dt.timezone.utc).isoformat()}
    if kind=='investment':
        asset_class=payload.get('assetClass','other')
        if asset_class not in ('stock','etf','bond','fund','other'):
            raise BadRequest('Classe investimento non valida')
        symbol=payload.get('symbol','')
        if not isinstance(symbol,str) or len(symbol)>20 or (symbol and not re.fullmatch(r'[A-Za-z0-9._-]+',symbol)):
            raise BadRequest('Ticker o ISIN non valido')
        asset.update({'assetClass':asset_class,'symbol':symbol.upper(),'currency':'EUR'})
        if payload.get('quantity') is not None or payload.get('unitPrice') is not None:
            quantity=positive_decimal(payload.get('quantity'),'Quantità')
            price=positive_decimal(payload.get('unitPrice'),'Prezzo unitario',6,True)
            calculated=int((quantity*price*100).quantize(Decimal('1'),rounding='ROUND_HALF_UP'))
            if calculated>100000000:
                raise BadRequest('Valore posizione oltre il limite')
            asset.update({'quantity':str(quantity),'unitPrice':str(price),
                          'amountCents':calculated})
        if payload.get('costBasis') is not None:
            asset['costBasisCents']=cents(payload['costBasis'],allow_zero=True)
        if payload.get('feesPaid') is not None:
            asset['feesPaidCents']=cents(payload['feesPaid'],allow_zero=True)
    return asset


def capital_public(row):
    item=public(row)
    if 'costBasisCents' in item:
        item['costBasis']=int(item.pop('costBasisCents'))/100
        item['unrealizedGain']=round((item['amount']-item['costBasis']),2)
        item['gainPct']=round(100*(item['amount']-item['costBasis'])/item['costBasis'],2) if item['costBasis']>0 else None
    if 'feesPaidCents' in item:
        item['feesPaid']=int(item.pop('feesPaidCents'))/100
    return item


def simulate_finances(sub, payload):
    """Scenari di cassa deterministici. Nessun rendimento implicito o dato esterno."""
    if not isinstance(payload,dict):
        raise BadRequest('Scenario non valido')
    months=payload.get('months',12)
    if type(months) is not int or not 1<=months<=36:
        raise BadRequest('Durata simulazione: da 1 a 36 mesi')
    contributions=cents(payload.get('monthlyReserve',0),allow_zero=True)
    added=cents(payload.get('extraExpenses',0),allow_zero=True)
    reduction=cents(payload.get('incomeReduction',0),allow_zero=True)
    unexpected=cents(payload.get('oneTimeExpense',0),allow_zero=True)
    state=state_for(user_key(sub))
    today=dt.datetime.now(ZoneInfo('Europe/Rome')).date()
    tx=[t for t in state['transactions'] if t['date']<=today.isoformat()]
    liquid=int(round(Decimal(str(accounts_state(user_key(sub))['netLiquidity']))*100))
    first=today.replace(day=1)
    # Ultimi tre mesi COMPLETI, senza trasferimenti e storni.
    sample=[]
    for i in (3,2,1):
        start=month_offset(first,-i).isoformat()
        end=month_offset(first,-i+1).isoformat()
        rows=[t for t in tx if start<=t['date']<end and t.get('affectsBalance',True)
              and not t.get('isTransfer') and not t.get('isCorrection') and not t.get('isReversed')]
        sample.append((sum(round(Decimal(str(t['amount']))*100) for t in rows if t['type']=='income'),
                       sum(round(Decimal(str(t['amount']))*100) for t in rows if t['type']=='expense')))
    avg_income=sum(n[0] for n in sample)//3
    avg_expenses=sum(n[1] for n in sample)//3
    original=liquid
    changed=liquid
    reserve=0
    rows=[]
    for n in range(1,months+1):
        original+=avg_income-avg_expenses
        changed+=max(0,avg_income-reduction)-avg_expenses-added-(unexpected if n==1 else 0)-contributions
        reserve+=contributions
        current=month_offset(first,n)
        rows.append({'month':current.strftime('%Y-%m'),'baseline':original/100,
                     'available':changed/100,'reserved':reserve/100,
                     'combined':(changed+reserve)/100})
    recorded_months=sum(any(x>0 for x in pair) for pair in sample)
    return {'initialLiquidity':liquid/100,'monthlyIncomeEstimate':avg_income/100,
            'monthlyExpenseEstimate':avg_expenses/100,'monthsWithData':recorded_months,
            'scenario':{'months':months,'monthlyReserve':contributions/100,
                        'extraExpenses':added/100,'incomeReduction':reduction/100,
                        'oneTimeExpense':unexpected/100},'projection':rows,
            'warnings':(['Sono disponibili pochi mesi con movimenti: la simulazione può essere poco rappresentativa.']
                        if recorded_months<3 else [])+
                       (['La liquidità disponibile scende sotto zero nel periodo simulato.']
                        if any(r['available']<0 for r in rows) else []),
            'assumptions':['Media aritmetica degli ultimi tre mesi completi, inclusi eventuali mesi senza registrazioni.',
                           'Nessuna rivalutazione degli investimenti, inflazione, tasse aggiuntive o rendimento ipotizzato.',
                           'La riserva mensile è un trasferimento interno: aumenta la riserva e riduce la cassa disponibile.',
                           'Le scadenze future non vengono aggiunte separatamente per evitare il doppio conteggio con le medie.']}



# Moneta 2.3.2: analisi dei consumi, regole trasparenti e nessuna API esterna.
ESSENTIAL_CATEGORIES = frozenset(('Casa','Affitto e mutuo','Condominio','Alimentari','Bollette',
    'Telefonia e Internet','Trasporti','Carburante','Auto e manutenzione',
    'Assicurazioni','Salute','Farmacia','Istruzione','Tasse','Spese bancarie'))

def spending_diagnostics(transactions, today):
    """Analisi limitata ai tre mesi *completi* e al mese in corso.

    Non considera trasferimenti, storni e righe non effettive. Le stime non
    includono pagamenti futuri e non interpretano una spesa come risparmio.
    """
    first=today.replace(day=1)
    window=month_offset(first,-3)
    expenses=[]; income=[]
    for t in transactions:
        if not (window.isoformat()<=t.get('date','')<=today.isoformat()):
            continue
        if (t.get('affectsBalance',True) is False or t.get('isTransfer') or
                t.get('isCorrection') or t.get('isReversed')):
            continue
        cents_value=int((Decimal(str(t['amount']))*100).to_integral_value())
        if t['type']=='expense': expenses.append((t['date'],t['category'],cents_value))
        elif t['type']=='income': income.append((t['date'],cents_value))
    completed_exp=[(d,c,a) for d,c,a in expenses if d<first.isoformat()]
    current_exp=[(d,c,a) for d,c,a in expenses if d>=first.isoformat()]
    completed_income=sum(a for d,a in income if d<first.isoformat())
    by_category={}
    for _,category,value in completed_exp:
        by_category[category]=by_category.get(category,0)+value
    total=sum(by_category.values())
    monthly_income=completed_income//3
    monthly_out=total//3
    discretionary=sum(a for c,a in by_category.items() if c not in ESSENTIAL_CATEGORIES)
    months_with_expenses=len({d[:7] for d,_,_ in completed_exp})
    top=sorted(by_category.items(),key=lambda row:(-row[1],row[0]))[:8]
    suggestions=[]
    if months_with_expenses<2 or completed_income<=0:
        suggestions.append({'type':'info','title':'Completa il quadro delle spese',
            'description':'Registra almeno due mesi di uscite e le entrate per ottenere confronti affidabili. Non aggiungere movimenti fittizi al tuo conto.'})
    else:
        ratio=(monthly_income-monthly_out)/monthly_income*100 if monthly_income else 0
        if ratio<0:
            suggestions.append({'type':'warning','title':'Spendi più di quanto incassi',
                'description':f'Negli ultimi tre mesi completi: entrate medie {monthly_income/100:.2f} €/mese, spese {monthly_out/100:.2f} €/mese. Differenza {((monthly_income-monthly_out)/100):.2f} €/mese. Parti dalle categorie più pesanti.'})
        elif ratio<10:
            suggestions.append({'type':'warning','title':'Margine di risparmio ridotto',
                'description':f'Risparmio stimato {ratio:.1f}% delle entrate medie degli ultimi tre mesi completi. Una prima soglia da monitorare può essere il 10%, se compatibile con le spese essenziali.'})
        if discretionary:
            target=(discretionary//3)*15//100
            suggestions.append({'type':'info','title':'Prova un taglio del 15% alle spese flessibili',
                'description':f'Le categorie non essenziali pesano {discretionary/300:.2f} €/mese in media. Una riduzione del 15% libererebbe circa {target/100:.2f} €/mese, se realmente sostenibile.'})
        if top:
            biggest,big_cents=top[0]
            suggestions.append({'type':'info','title':'La categoria più costosa: '+biggest,
                'description':f'Negli ultimi tre mesi hai registrato {big_cents/100:.2f} € in {biggest} ({big_cents*100/total:.0f}% delle spese). Apri i movimenti di questa categoria e verifica le operazioni ripetute.'})
    return {'monthsWithExpenses':months_with_expenses,'monthlyIncome':monthly_income/100,
        'monthlyExpenses':monthly_out/100,'monthlySavings':(monthly_income-monthly_out)/100,
        'savingsRatePercent':round((monthly_income-monthly_out)*100/monthly_income,1) if monthly_income else None,
        'currentMonthExpenses':sum(a for _,_,a in current_exp)/100,
        'monthlyDiscretionary':round(discretionary/300,2),
        'categories':[{'name':cat,'total':value/100,'monthlyAverage':round(value/300,2),
                      'percentage':round(value*100/total,1),'essential':cat in ESSENTIAL_CATEGORIES}
                      for cat,value in top],
        'suggestions':suggestions,
        'periodStart':window.isoformat(),'periodEnd':(first-dt.timedelta(days=1)).isoformat()}

def private_insights(sub):
    pk=user_key(sub)
    state=state_for(pk)
    transactions=state['transactions']
    today=dt.datetime.now(ZoneInfo('Europe/Rome')).date()
    relevant=[t for t in transactions if t['date']<=today.isoformat()]
    liquid=int(round(Decimal(str(accounts_state(pk)['netLiquidity']))*100))
    assets=[capital_public(r['value']) for r in list_prefix(pk,'ASSET#')]
    totals={kind:sum(round(Decimal(str(a['amount']))*100) for a in assets if a['kind']==kind)
            for kind in ('investment','other','cash_outside_ledger','debt')}
    net=liquid+totals['investment']+totals['other']+totals['cash_outside_ledger']-totals['debt']
    first=(today.replace(day=1)-dt.timedelta(days=1)).replace(day=1)
    monthly=[]
    for i in range(3):
        end=month_offset(first,-i+1)
        begin=month_offset(first,-i)
        group=[t for t in relevant if begin.isoformat()<=t['date']<end.isoformat()
               and t.get('affectsBalance',True) and not t.get('isTransfer',False) and not t.get('isCorrection',False)
               and not t.get('isReversed',False)]
        income=sum(round(Decimal(str(t['amount']))*100) for t in group if t['type']=='income')
        spent=sum(round(Decimal(str(t['amount']))*100) for t in group if t['type']=='expense')
        monthly.append({'month':begin.strftime('%Y-%m'),'income':income/100,'expenses':spent/100,
                        'saved':(income-spent)/100})
    essentials = ESSENTIAL_CATEGORIES
    expense_sample=[round(Decimal(str(t['amount']))*100) for t in relevant
                   if t['type']=='expense' and t.get('affectsBalance',True)
                   and not t.get('isTransfer',False) and not t.get('isCorrection',False)
                   and not t.get('isReversed',False) and t.get('category') in essentials
                   and month_offset(first,1).isoformat() > t['date'] >= month_offset(first,-2).isoformat()]
    essential_avg = sum(expense_sample)//3
    coverage=(round(liquid/essential_avg,1) if essential_avg>0 and liquid>=0 else None)
    notices=[]
    if liquid<0:
        notices.append({'type':'warning','title':'Saldo disponibile negativo',
                        'description':'Verifica movimenti, saldo iniziale e prossime scadenze.'})
    if essential_avg==0:
        notices.append({'type':'info','title':'Spese essenziali non ancora stimabili',
                        'description':'Servono movimenti categorizzati degli ultimi tre mesi completi.'})
    elif coverage is not None:
        notices.append({'type':'info','title':'Copertura delle spese essenziali',
                        'description':f'Liquidità registrata: circa {coverage} mesi al ritmo medio recente. Non è una garanzia futura.'})
    # Solo compatibilità API: nessuna scheda investimenti nella nuova interfaccia.
    portfolio=[]
    invested=totals['investment']
    for item in assets:
        if item['kind']!='investment':
            continue
        share=(int(round(Decimal(str(item['amount']))*100))*100/invested) if invested else 0
        age=(today-date_value(item['asOf'])).days
        portfolio.append({'id':item['id'],'name':item['name'],'value':item['amount'],
                          'weightPercent':round(share,1),'asOf':item['asOf'],
                          'daysSinceValuation':age,'unrealizedGain':item.get('unrealizedGain'),
                          'gainPct':item.get('gainPct'),'assetClass':item.get('assetClass','other')})
    # Suggerimenti sul budget: soglie verificabili rispetto al mese completo precedente.
    previous=first.isoformat()
    last_end=month_offset(first,1).isoformat()
    for category, amount in state['budgets'].items():
        limit=round(Decimal(str(amount))*100)
        if not limit:
            continue
        out=sum(round(Decimal(str(t['amount']))*100) for t in relevant
                if previous<=t['date']<last_end and t['type']=='expense'
                and t.get('category')==category and t.get('affectsBalance',True)
                and not t.get('isTransfer') and not t.get('isReversed')
                and not t.get('isCorrection'))
        if out >= limit:
            notices.append({'type':'warning','title':'Budget superato · '+category,
                            'description':f"Nel mese scorso hai speso {out/100:.2f} € rispetto a {limit/100:.2f} € di budget."})
        elif out >= limit*8//10:
            notices.append({'type':'info','title':'Budget vicino al limite · '+category,
                            'description':f"Nel mese scorso hai utilizzato circa {out/limit*100:.0f}% del budget."})
    spending_analysis=spending_diagnostics(relevant, today)
    notices=spending_analysis['suggestions']+notices
    return {'liquidity':liquid/100,'netWorth':net/100,'assets':assets,
            'totals':{k:v/100 for k,v in totals.items()},
            'lastThreeMonths':monthly,'spending':spending_analysis,'essentialAverage':essential_avg/100,
            'coverageMonths':coverage,'notices':notices,'portfolio':portfolio,
            'assumptions':['La liquidità netta include banca, contanti, conto deposito e debiti delle carte a saldo.',
                'Non duplicare gli importi del nuovo registro Contanti negli asset di liquidità esterni.',
                'Nessuna quota del fondo condiviso è attribuita automaticamente al patrimonio personale.',
                'Versamenti e rimborsi tracciati non sono reddito o spesa nei consigli.']}



# --- Moneta 2.3: conti, contanti, carte a saldo differito ---
DEFAULT_ACCOUNT_IDS = frozenset(('bank','cash','deposit'))
ACCOUNT_LIMIT = 8

def account_id(value):
    if not isinstance(value,str) or not ID_RE.fullmatch(value):
        raise BadRequest('Conto di pagamento non valido')
    return value

def money_cents(value, allow_zero=True):
    return cents(value, allow_zero=allow_zero)

def user_accounts(pk):
    accounts = {
        'bank':{'id':'bank','name':'Conto corrente','kind':'bank','openingCents':0},
        'cash':{'id':'cash','name':'Contanti','kind':'cash','openingCents':0},
        'deposit':{'id':'deposit','name':'Conto deposito','kind':'deposit','openingCents':0},
    }
    for row in list_prefix(pk,'ACCOUNT#'):
        if not row.get('SK','').startswith('ACCOUNT#') or not row.get('value'):
            continue
        value = dict(row['value'])
        # boto3 deserializes DynamoDB Number fields as Decimal, including the
        # integral calendar parameters saved by the card configuration form.
        # Normalize at the persistence boundary so all consumers (cycle dates,
        # editor, balance calculations, scheduler) receive actual integers.
        for field in ('openingCents', 'closingDay', 'chargeDay', 'chargeMonthOffset'):
            if field in value:
                number = value[field]
                if isinstance(number, bool) or not isinstance(number, (int, Decimal)):
                    raise BadRequest('Configurazione conto non valida: ' + field)
                if isinstance(number, Decimal) and (not number.is_finite() or number != number.to_integral_value()):
                    raise BadRequest('Configurazione conto non valida: ' + field)
                value[field] = int(number)
        accounts[value['id']] = value
    return accounts

def account_for(pk, ident, expense=False):
    account = user_accounts(pk).get(account_id(ident))
    if not account or account.get('inactive'):
        raise BadRequest('Conto inesistente o disattivato')
    if account['kind']=='deposit':
        raise BadRequest('Il conto deposito ammette solo trasferimenti e saldo iniziale')
    if account['kind']=='credit' and not expense:
        raise BadRequest('Le entrate non possono essere accreditate su carta a saldo')
    return account

def account_record(payload, ident=None, previous=None):
    if ident is None:
        ident = account_id(payload.get('id'))
        if ident in DEFAULT_ACCOUNT_IDS:
            raise BadRequest('Identificativo riservato')
        kind = 'credit'
    else:
        kind = (previous or {}).get('kind')
        if kind not in ('bank','cash','deposit','credit'):
            raise BadRequest('Tipo conto non valido')
    name = payload.get('name', (previous or {}).get('name',''))
    if not isinstance(name,str) or not 2 <= len(name.strip()) <= 60:
        raise BadRequest('Nome conto non valido')
    opening = money_cents(payload.get('openingBalance',(previous or {}).get('openingCents',0)/100))
    if kind!='credit':
        return {'id':ident,'name':name.strip(),'kind':kind,'openingCents':opening}
    closing = payload.get('closingDay',(previous or {}).get('closingDay',25))
    charge = payload.get('chargeDay',(previous or {}).get('chargeDay',10))
    offset = payload.get('chargeMonthOffset',(previous or {}).get('chargeMonthOffset',1))
    mode = payload.get('settlementMode',(previous or {}).get('settlementMode','confirm'))
    if type(closing) is not int or type(charge) is not int or not 1<=closing<=31 or not 1<=charge<=31:
        raise BadRequest('Giorni della carta non validi')
    if type(offset) is not int or offset not in (0,1):
        raise BadRequest('Mese di addebito non valido')
    if offset == 0 and (charge <= closing or charge > 28):
        raise BadRequest('Per un addebito nello stesso mese scegli un giorno successivo alla chiusura, entro il 28')
    if mode not in ('confirm','automatic'):
        raise BadRequest('Modalità di addebito non valida')
    # Le modifiche automatiche non sono retroattive.
    auto_from = (previous or {}).get('autoFrom') if mode=='automatic' and (previous or {}).get('settlementMode')=='automatic' else None
    if mode=='automatic' and not auto_from:
        auto_from = dt.datetime.now(ZoneInfo('Europe/Rome')).date().isoformat()
    return {'id':ident,'name':name.strip(),'kind':'credit','openingCents':0,
            'closingDay':closing,'chargeDay':charge,'chargeMonthOffset':offset,
            'settlementMode':mode,'autoFrom':auto_from}

def close_date(account, cycle):
    first = date_value(cycle+'-01')
    return first.replace(day=min(account['closingDay'],calendar.monthrange(first.year,first.month)[1]))

def card_cycle(account, date):
    date = date_value(date) if isinstance(date,str) else date
    cycle_start = date.replace(day=1)
    if date > close_date(account,cycle_start.strftime('%Y-%m')):
        cycle_start = month_offset(cycle_start,1)
    return cycle_start.strftime('%Y-%m')

def card_due(account, cycle):
    base = month_offset(date_value(cycle+'-01'),account['chargeMonthOffset'])
    return base.replace(day=min(account['chargeDay'],calendar.monthrange(base.year,base.month)[1])).isoformat()

def settlement_sk(card,cycle):
    return 'SETTLE#'+card+'#'+cycle

def cycle_sk(card, cycle):
    return 'CYCLE#'+card+'#'+cycle

def card_transaction_guard(pk, record, previous=None):
    """Maintain a revisioned card-cycle total in the *same transaction* as every purchase.

    Settlement checks this revision atomically, so a purchase racing with a
    settlement cannot be omitted from the billed amount. A settled cycle remains
    immutable. The operation also validates edits that move a charge between cycles.
    """
    if all(not rec or rec.get('accountId','bank') in DEFAULT_ACCOUNT_IDS for rec in (record,previous)):
        return []
    accounts=user_accounts(pk)
    touched={}
    for rec,sign in ((previous,-1),(record,1)):
        if not rec:
            continue
        card=accounts.get(rec.get('accountId','bank'))
        if not card or card['kind']!='credit':
            continue
        key=(card['id'],card_cycle(card,rec['date']))
        touched[key]=touched.get(key,0)+sign*int(rec['amountCents'])*(1 if rec['type']=='expense' else -1)
    checks=[]
    for (ident,cycle),delta in touched.items():
        checks.append({'ConditionCheck':{'TableName':TABLE.name,
            'Key':wire({'PK':pk,'SK':settlement_sk(ident,cycle)}),
            'ConditionExpression':'attribute_not_exists(PK)'}})
        if delta:
            checks.append({'Update':{'TableName':TABLE.name,
                'Key':wire({'PK':pk,'SK':cycle_sk(ident,cycle)}),
                'UpdateExpression':'ADD chargeCents :delta, revision :one',
                'ExpressionAttributeValues':wire({':delta':delta,':one':1})}})
    return checks

def card_cycles(pk, rows, accounts):
    cycles={}
    for row in rows:
        if not row['SK'].startswith('TX#') or row.get('skipped'):
            continue
        t=row['value']; ident=t.get('accountId','bank')
        if ident not in accounts or accounts[ident]['kind']!='credit' or t.get('affectsBalance',True) is False:
            continue
        cycle=card_cycle(accounts[ident],t['date'])
        cycles[(ident,cycle)]=cycles.get((ident,cycle),0)+(t['amountCents'] if t['type']=='expense' else -t['amountCents'])
    today=dt.datetime.now(ZoneInfo('Europe/Rome')).date().isoformat()
    out=[]
    for (ident,cycle),amount in sorted(cycles.items(),reverse=True):
        if amount <= 0: continue
        row=next((r for r in rows if r['SK']==settlement_sk(ident,cycle)),None)
        paid=int(row['value']['amountCents']) if row else 0
        due=card_due(accounts[ident],cycle)
        out.append({'cardId':ident,'cycle':cycle,'closingDate':close_date(accounts[ident],cycle).isoformat(),
                    'dueDate':due,'amount':amount/100,'paid':paid/100,'remaining':(amount-paid)/100,
                    'status':'paid' if paid else ('overdue' if due<today else 'scheduled'),
                    'paidDate':row['value'].get('paidDate') if row else None})
    return out

def accounts_state(pk):
    rows=query_user(pk)
    accounts=user_accounts(pk)
    balances={a:int(v.get('openingCents',0)) for a,v in accounts.items()}
    for row in rows:
        sk=row['SK']
        if sk.startswith('TX#') and not row.get('skipped'):
            t=row['value']
            if t.get('affectsBalance',True) is False or t['date']>dt.datetime.now(ZoneInfo('Europe/Rome')).date().isoformat():
                continue
            ident=t.get('accountId','bank')
            if ident in balances:
                balances[ident]+=(1 if t['type']=='income' else -1)*int(t['amountCents'])
        elif sk.startswith('XFER#'):
            t=row['value']
            if t['date']<=dt.datetime.now(ZoneInfo('Europe/Rome')).date().isoformat():
                balances[t['fromAccountId']]-=int(t['amountCents'])
                balances[t['toAccountId']]+=int(t['amountCents'])
        elif sk.startswith('SETTLE#'):
            payment=row['value']
            if payment['paidDate']<=dt.datetime.now(ZoneInfo('Europe/Rome')).date().isoformat():
                balances['bank']-=int(payment['amountCents'])
                balances[payment['cardId']]+=int(payment['amountCents'])
    cycles=card_cycles(pk,rows,accounts)
    result=[]
    for key,item in accounts.items():
        result.append({k:v for k,v in item.items() if k!='openingCents'} |
                      {'openingBalance':item.get('openingCents',0)/100,'balance':balances[key]/100})
    reversed_transfers={r['SK'][8:] for r in rows if r['SK'].startswith('REVXFER#')}
    transfers=[{**r['value'],'amount':r['value']['amountCents']/100,
                'reversed':r['value']['id'] in reversed_transfers} for r in rows if r['SK'].startswith('XFER#')]
    transfers.sort(key=lambda t:(t['date'],t['id']),reverse=True)
    bank=balances.get('bank',0);cash=balances.get('cash',0);deposit=balances.get('deposit',0)
    debt=sum(-balances[k] for k,v in accounts.items() if v['kind']=='credit')
    return {'accounts':result,'cycles':cycles[:150],'transfers':transfers[:100],
            'bankBalance':bank/100,'cashBalance':cash/100,'depositBalance':deposit/100,
            'creditDebt':debt/100,'spendable':(bank+cash)/100,
            'available':(bank+cash+deposit)/100,'netLiquidity':(bank+cash+deposit-debt)/100}

def settle_card(pk,card_id,cycle,paid_date,automated=False):
    card=account_for(pk,card_id,expense=True)
    if card['kind']!='credit': raise BadRequest('Seleziona una carta di credito')
    if not re.fullmatch(r'\d{4}-\d{2}',cycle or ''):
        raise BadRequest('Ciclo di addebito non valido')
    date_value(cycle+'-01')
    paid=date_value(paid_date).isoformat()
    if close_date(card,cycle).isoformat() > paid:
        raise BadRequest('Non puoi saldare un estratto conto prima della chiusura')
    if paid > dt.datetime.now(ZoneInfo('Europe/Rome')).date().isoformat():
        raise BadRequest('Data di addebito futura: attendi la scadenza')
    rows=query_user(pk)
    cycles=card_cycles(pk,rows,user_accounts(pk))
    found=next((x for x in cycles if x['cardId']==card_id and x['cycle']==cycle),None)
    if not found or found['status']=='paid':
        raise BadRequest('Estratto conto già saldato o senza movimenti')
    amount=int(Decimal(str(found['amount']))*100)
    key=settlement_sk(card_id,cycle)
    payment={'cardId':card_id,'cycle':cycle,'amountCents':amount,'paidDate':paid,'automatic':automated,
             'createdAt':dt.datetime.now(dt.timezone.utc).isoformat()}
    current_cycle=get_row(pk,cycle_sk(card_id,cycle))
    if not current_cycle or int(current_cycle.get('chargeCents',0)) != amount:
        raise BadRequest('Importo del ciclo non allineato: aggiorna i dati e riprova')
    expected_revision=int(current_cycle['revision'])
    # A purchase and its cycle total update are atomic. Settlement includes a
    # consistent revision check and cannot miss a concurrently recorded charge.
    txn([{'ConditionCheck':{'TableName':TABLE.name,
                            'Key':wire({'PK':pk,'SK':cycle_sk(card_id,cycle)}),
                            'ConditionExpression':'revision = :r AND chargeCents = :amount',
                            'ExpressionAttributeValues':wire({':r':expected_revision,':amount':amount})}},
         put_action({'PK':pk,'SK':key,'value':payment})])
    return {'cardId':card_id,'cycle':cycle,'amount':amount/100,'paidDate':paid,'automatic':automated}

def run_auto_card_settlements(today):
    processed=0;last=None
    while True:
        params={'FilterExpression':'begins_with(SK,:p)','ExpressionAttributeValues':{':p':'ACCOUNT#'},'ProjectionExpression':'PK,SK,#v',
                'ExpressionAttributeNames':{'#v':'value'}}
        if last:params['ExclusiveStartKey']=last
        result=TABLE.scan(**params)
        for row in result.get('Items',[]):
            card=row['value']
            if card.get('kind')!='credit' or card.get('settlementMode')!='automatic':continue
            pk=row['PK']
            for cycle in card_cycles(pk,query_user(pk),user_accounts(pk)):
                if cycle['cardId']!=card['id'] or cycle['status']=='paid':continue
                if cycle['dueDate']>today.isoformat() or cycle['dueDate']<card.get('autoFrom','9999-12-31'):continue
                try:
                    settle_card(pk,card['id'],cycle['cycle'],cycle['dueDate'],automated=True)
                    processed+=1
                except (BadRequest,ClientError):
                    LOG.warning('Non posso addebitare automaticamente la carta %s',card['id'])
        last=result.get('LastEvaluatedKey')
        if not last: break
    return processed

def state_for(pk):
    tx, budgets, rules, planned = [], {}, [], []
    reversed_operations = set()
    for row in query_user(pk):
        sk = row['SK']
        if sk.startswith('TX#') and not row.get('skipped'):
            transaction = public(row['value'])
            tx.append(transaction)
            if transaction.get('reversesOperationId'):
                reversed_operations.add(transaction['reversesOperationId'])
        elif sk.startswith('B#'):
            budgets[sk[2:]] = int(row['amountCents'])/100
        elif sk.startswith('REC#'):
            rules.append(public(row['value']))
        elif sk.startswith('PLAN#'):
            planned.append(public(row['value']))
        elif sk.startswith('REV#'):
            reversed_operations.add(sk[4:])
    for transaction in tx:
        if transaction.get('ledgerOperationId') in reversed_operations:
            transaction['isReversed'] = True
    return {'transactions':tx,'budgets':budgets,'recurring':rules,'planned':planned}



def handle_request(event, claims, sub, pk, route, params):
    space_id = (event.get('queryStringParameters') or {}).get('spaceId') or None
    if route == 'GET /me':
        return response(200, {'userId':sub,'isAdmin':groups_admin(claims),
                              'spaces':user_spaces(sub),'invites':pending_invites(sub)})
    if route == 'POST /spaces':
        return response(201,new_space(sub,body_json(event)))
    if route == 'GET /spaces/{id}/members':
        return response(200,{'members':list_members(params.get('id'),sub)})
    if route == 'POST /spaces/{id}/invites':
        return response(201,invite_member(params.get('id'),sub,body_json(event)))
    if route == 'POST /invites/{id}/accept':
        return response(200,accept_invite(sub,params.get('id'),True))
    if route == 'POST /invites/{id}/decline':
        return response(200,accept_invite(sub,params.get('id'),False))
    if route == 'DELETE /spaces/{id}/members/{userId}':
        return response(200,remove_member(params.get('id'),sub,params.get('userId')))

    # Non basta un parametro dal frontend: ogni richiesta allo spazio richiede
    # una verifica del membro sul server. Le scritture condivise ripetono
    # la verifica nella stessa transazione che aggiorna i dati.
    if space_id:
        membership(space_id, sub, write=route.startswith(('PUT ', 'POST ', 'DELETE ')))
        data_pk = space_key(space_id)
    else:
        data_pk = pk
    # Operazioni sul solo patrimonio personale: mai su fondi condivisi.
    if route.startswith(('GET /accounts','POST /accounts','PUT /accounts','POST /transfers','POST /cards/')):
        if space_id: raise BadRequest('I conti appartengono esclusivamente al bilancio personale')
    if route == 'GET /accounts':
        return response(200,accounts_state(pk))
    if route == 'POST /accounts':
        payload=body_json(event)
        if sum(a['kind']=='credit' for a in user_accounts(pk).values())>=ACCOUNT_LIMIT:
            raise BadRequest('Numero massimo di carte raggiunto')
        item=account_record(payload)
        if item['kind']!='credit':raise BadRequest('Tipo conto non supportato')
        TABLE.put_item(Item={'PK':pk,'SK':'ACCOUNT#'+item['id'],'value':item},ConditionExpression='attribute_not_exists(PK)')
        return response(201,{'id':item['id'],'kind':item['kind']})
    if route == 'PUT /accounts/{id}':
        ident=account_id(params.get('id'))
        previous=user_accounts(pk).get(ident)
        if not previous: raise KeyError('Conto inesistente')
        payload=body_json(event)
        item=account_record(payload,ident,previous)
        if previous['kind']=='credit' and any(item[k]!=previous[k] for k in ('closingDay','chargeDay','chargeMonthOffset')):
            rows=query_user(pk)
            if any(r['SK'].startswith('SETTLE#'+ident+'#') or (r['SK'].startswith('TX#') and r.get('value',{}).get('accountId')==ident) for r in rows):
                raise BadRequest('Non puoi cambiare le date di una carta con movimenti: creane una nuova')
        condition='attribute_exists(PK)' if ident not in DEFAULT_ACCOUNT_IDS else None
        kw={'Item':{'PK':pk,'SK':'ACCOUNT#'+ident,'value':item}}
        if condition:kw['ConditionExpression']=condition
        TABLE.put_item(**kw)
        return response(200,{'id':ident,'kind':item['kind']})
    if route == 'POST /transfers':
        data=body_json(event)
        ident=account_id(data.get('id'))
        from_id=account_id(data.get('fromAccountId'))
        to_id=account_id(data.get('toAccountId'))
        if from_id==to_id:raise BadRequest('Origine e destinazione non possono coincidere')
        acc=user_accounts(pk)
        if any(x not in acc or acc[x]['kind'] not in ('bank','cash','deposit') for x in (from_id,to_id)):
            raise BadRequest('Trasferimenti consentiti solo fra conto corrente, contanti e deposito')
        amount=cents(data.get('amount'))
        note=data.get('note','')
        if not isinstance(note,str) or len(note)>200:raise BadRequest('Nota non valida')
        item={'id':ident,'fromAccountId':from_id,'toAccountId':to_id,'amountCents':amount,
              'date':date_value(data.get('date')).isoformat(),'note':note}
        TABLE.put_item(Item={'PK':pk,'SK':'XFER#'+ident,'value':item},ConditionExpression='attribute_not_exists(PK)')
        return response(201,{**item,'amount':amount/100})
    if route == 'POST /transfers/{id}/reverse':
        transfer_id=account_id(params.get('id'))
        original=get_row(pk,'XFER#'+transfer_id)
        if not original or original['value'].get('reversalOf'):
            raise KeyError('Trasferimento non trovato')
        reason=body_json(event).get('reason','')
        if not isinstance(reason,str) or not 8<=len(reason.strip())<=200:
            raise BadRequest('Motivo di storno obbligatorio (8–200 caratteri)')
        data=original['value']
        reversal={'id':'rev-'+transfer_id,'fromAccountId':data['toAccountId'],
                  'toAccountId':data['fromAccountId'],'amountCents':data['amountCents'],
                  'date':data['date'],'note':'Storno: '+reason.strip(),'reversalOf':transfer_id}
        marker={'PK':pk,'SK':'REVXFER#'+transfer_id,'reason':reason.strip(),
                'createdAt':dt.datetime.now(dt.timezone.utc).isoformat()}
        txn([put_action(marker),put_action({'PK':pk,'SK':'XFER#'+reversal['id'],'value':reversal})])
        return response(201,{'reversed':True,'originalId':transfer_id})
    if route == 'POST /cards/{id}/cycles/{cycle}/settle':
        payload=body_json(event)
        result=settle_card(pk,account_id(params.get('id')),params.get('cycle'),payload.get('paidDate'))
        return response(201,result)
    if route == 'GET /fund':
        if not space_id:
            raise BadRequest('Seleziona un fondo condiviso')
        return response(200,read_fund(space_id,sub))
    if route == 'POST /fund/operations':
        if not space_id:
            raise BadRequest('Seleziona un fondo condiviso')
        return response(201,create_fund_operation(space_id,sub,body_json(event)))
    if route == 'POST /fund/operations/{id}/reverse':
        if not space_id:
            raise BadRequest('Seleziona un fondo condiviso')
        return response(201,reverse_fund_operation(space_id,sub,params.get('id'),body_json(event)))
    if route == 'POST /insights/simulate':
        if space_id:
            raise BadRequest('Simulazioni riservate al bilancio personale')
        return response(200,simulate_finances(sub,body_json(event)))
    if route == 'GET /insights':
        if space_id:
            raise BadRequest('Gli Insights patrimoniali sono disponibili solo nel bilancio personale')
        return response(200,private_insights(sub))
    if route == 'POST /capital/items':
        if space_id:
            raise BadRequest('Il patrimonio personale non appartiene al fondo condiviso')
        item=capital_item(body_json(event))
        if len(list_prefix(pk,'ASSET#'))>=CAPITAL_ITEM_LIMIT:
            raise BadRequest('Numero massimo di elementi patrimoniali raggiunto')
        TABLE.put_item(Item={'PK':pk,'SK':'ASSET#'+item['id'],'value':item},
                       ConditionExpression='attribute_not_exists(PK)')
        return response(201,capital_public(item))
    if route == 'PUT /capital/items/{id}':
        if space_id:
            raise BadRequest('Il patrimonio personale non appartiene al fondo condiviso')
        ident=safe_id(params.get('id'))
        old=get_row(pk,'ASSET#'+ident)
        if not old:
            raise KeyError('Elemento patrimoniale inesistente')
        payload=body_json(event)
        expected=payload.get('revision')
        if type(expected) is not int or expected != int(old['value'].get('revision',1)):
            raise BadRequest('Versione dell’elemento patrimoniale non aggiornata')
        item=capital_item(payload,ident,expected+1)
        # Il controllo della versione è necessario se due dispositivi aggiornano insieme.
        condition=('#v.#revision = :old' if 'revision' in old['value']
                   else 'attribute_exists(PK) AND attribute_not_exists(#v.#revision)')
        args={'Item':{'PK':pk,'SK':'ASSET#'+ident,'value':item},
              'ConditionExpression':condition,'ExpressionAttributeNames':{'#v':'value'}}
        if 'revision' in old['value']:
            args['ExpressionAttributeValues']={':old':expected}
        TABLE.put_item(**args)
        return response(200,capital_public(item))
    if route == 'DELETE /capital/items/{id}':
        if space_id:
            raise BadRequest('Il patrimonio personale non appartiene al fondo condiviso')
        ident=safe_id(params.get('id'))
        if not get_row(pk,'ASSET#'+ident):
            raise KeyError('Elemento patrimoniale inesistente')
        TABLE.delete_item(Key={'PK':pk,'SK':'ASSET#'+ident},
                          ConditionExpression='attribute_exists(PK)')
        return response(204)
    if route == 'GET /favorites':
        return response(200,{'favorites':favorites_for(sub,space_id)})
    if route == 'POST /favorites':
        record = validated_favorite(body_json(event))
        if record['spaceId'] != (space_id or ''):
            raise BadRequest('Il preferito deve appartenere al bilancio selezionato')
        if len(list_prefix(pk,'FAV#')) >= FAVORITES_LIMIT:
            raise BadRequest('Numero massimo di preferiti raggiunto')
        TABLE.put_item(Item={'PK':pk,'SK':'FAV#'+record['id'],'value':record},
                       ConditionExpression='attribute_not_exists(PK)')
        return response(201,public(record))
    if route == 'DELETE /favorites/{id}':
        fid = params.get('id')
        if not isinstance(fid,str) or not ID_RE.fullmatch(fid):
            raise BadRequest('ID preferito non valido')
        old = get_row(pk,'FAV#'+fid)
        if not old or old['value'].get('spaceId','') != (space_id or ''):
            raise KeyError('Preferito inesistente')
        TABLE.delete_item(Key={'PK':pk,'SK':'FAV#'+fid},ConditionExpression='attribute_exists(PK)')
        return response(204)
    if route in ('POST /planned','PUT /planned/{id}'):
        rid = params.get('id') if route.startswith('PUT') else None
        payload = planned_record(body_json(event),rid)
        if not space_id or payload.get('paymentSource') == 'personal':
            account_for(pk,payload.get('accountId','bank'),expense=payload['type']=='expense')
        old = get_row(data_pk,'PLAN#'+rid) if rid else None
        if rid and (not old or old['value'].get('status')!='scheduled'):
            raise BadRequest('Puoi modificare solo scadenze ancora da pagare')
        if not rid and len(list_prefix(data_pk,'PLAN#')) >= PLANNED_LIMIT:
            raise BadRequest('Numero massimo di scadenze raggiunto')
        cond = '#v.#status = :scheduled' if rid else 'attribute_not_exists(PK)'
        row = {'PK':data_pk,'SK':'PLAN#'+payload['id'],'value':payload}
        if space_id:
            action = put_action(row,cond,{':scheduled':'scheduled'} if rid else None)
            if rid:
                action['Put']['ExpressionAttributeNames']={'#v':'value','#status':'status'}
            txn([auth_check(space_id,sub),action])
        else:
            params={'Item':row,'ConditionExpression':cond}
            if rid:
                params['ExpressionAttributeNames']={'#v':'value','#status':'status'}
                params['ExpressionAttributeValues']={':scheduled':'scheduled'}
            TABLE.put_item(**params)
        return response(200 if rid else 201,public(payload))
    if route == 'DELETE /planned/{id}':
        rid = params.get('id')
        if not isinstance(rid,str) or not ID_RE.fullmatch(rid):
            raise BadRequest('ID scadenza non valido')
        old = get_row(data_pk,'PLAN#'+rid)
        if not old or old['value'].get('status') != 'scheduled':
            raise BadRequest('Puoi eliminare solo scadenze ancora da pagare')
        cond='#v.#status = :scheduled'
        if space_id:
            action = del_action(data_pk,'PLAN#'+rid,cond,{':scheduled':'scheduled'})
            action['Delete']['ExpressionAttributeNames']={'#v':'value','#status':'status'}
            txn([auth_check(space_id,sub),action])
        else:
            TABLE.delete_item(Key={'PK':data_pk,'SK':'PLAN#'+rid},ConditionExpression=cond,
                              ExpressionAttributeNames={'#v':'value','#status':'status'},
                              ExpressionAttributeValues={':scheduled':'scheduled'})
        return response(204)
    if route == 'POST /planned/{id}/complete':
        return response(200,complete_planned(data_pk,space_id,sub,params.get('id'),body_json(event).get('paidDate')))
    if route == 'GET /state':
        return response(200,state_for(data_pk))
    # Defence-in-depth: route amministrative NON eseguite dalla Lambda finanziaria.
    if route.startswith(('GET /admin/', 'POST /admin/')) or route == 'GET /costs':
        raise PermissionError('Endpoint amministrativo isolato')
    if route in ('POST /transactions','PUT /transactions/{id}'):
        rid = params.get('id') if route.startswith('PUT') else None
        payload = body_json(event)
        record = validate_record(payload,rid)
        if not space_id or payload.get('paymentSource')=='personal':
            account_for(pk,record['accountId'],expense=record['type']=='expense')
        if space_id:
            share_record(record, payload.get('paymentSource','fund'))
            method = 'update' if rid else 'create'
            return response(200 if rid else 201,public(transaction_actions(space_id,sub,record,method)))
        if rid:
            previous = get_row(pk,'TX#'+rid) or {}
            if previous.get('skipped'):
                raise KeyError('Movimento eliminato')
            if (previous.get('value') or {}).get('ledgerOperationId'):
                return response(403,{'error':'Movimento contabile non modificabile'})
            if (previous.get('value') or {}).get('linkedSpaceId'):
                return response(403,{'error':'Modifica il movimento nel bilancio condiviso originale'})
            rec_id = (previous.get('value') or {}).get('recurringId')
            if rec_id:
                record['recurringId'] = rec_id
        try:
            checks=card_transaction_guard(pk,record,(previous.get('value') if rid else None))
            if checks:
                put=put_action({'PK':pk,'SK':'TX#'+record['id'],'value':record},
                               'attribute_exists(PK)' if rid else 'attribute_not_exists(PK)')
                txn(checks+[put])
            else:
                TABLE.put_item(Item={'PK':pk,'SK':'TX#'+record['id'],'value':record},
                               ConditionExpression='attribute_exists(PK)' if rid else 'attribute_not_exists(PK)')
        except ClientError as err:
            if err.response['Error']['Code'] == 'ConditionalCheckFailedException':
                return response(404 if rid else 409,{'error':'Movimento inesistente o duplicato'})
            raise
        return response(200 if rid else 201,public(record))
    if route in ('DELETE /transactions/{id}','DELETE /recurring/{id}'):
        rid = params.get('id')
        if not isinstance(rid,str) or not ID_RE.fullmatch(rid):
            raise BadRequest('ID non valido')
        if space_id and route.startswith('DELETE /transactions'):
            delete_shared_transaction(space_id,sub,rid)
            return response(204)
        sk = ('TX#' if 'transactions' in route else 'REC#') + rid
        old = get_row(data_pk,sk) or {}
        if sk.startswith('TX#'):
            if old.get('skipped'):
                return response(204)
            if (old.get('value') or {}).get('ledgerOperationId'):
                return response(403,{'error':'Movimento contabile non eliminabile'})
            if not space_id and (old.get('value') or {}).get('linkedSpaceId'):
                return response(403,{'error':'Elimina il movimento dal bilancio condiviso'})
            if (old.get('value') or {}).get('recurringId'):
                if space_id:
                    txn([auth_check(space_id,sub),put_action({'PK':data_pk,'SK':sk,'skipped':True},'attribute_exists(PK)')])
                else:
                    prior=old.get('value')
                    guards=card_transaction_guard(pk,None,prior) if prior else []
                    if guards:
                        txn(guards+[put_action({'PK':data_pk,'SK':sk,'skipped':True},'attribute_exists(PK)')])
                    else:
                        TABLE.put_item(Item={'PK':data_pk,'SK':sk,'skipped':True},ConditionExpression='attribute_exists(PK)')
                return response(204)
        try:
            if space_id:
                txn([auth_check(space_id,sub),del_action(data_pk,sk)])
            else:
                original=(old.get('value') if sk.startswith('TX#') else None)
                if original and user_accounts(pk).get(original.get('accountId','bank'),{}).get('kind')=='credit':
                    txn(card_transaction_guard(pk,None,original)+[del_action(data_pk,sk)])
                else:
                    TABLE.delete_item(Key={'PK':data_pk,'SK':sk},ConditionExpression='attribute_exists(PK)')
        except ClientError as err:
            if err.response['Error']['Code'] == 'ConditionalCheckFailedException':
                return response(404,{'error':'Elemento inesistente'})
            raise
        return response(204)
    if route in ('POST /recurring','PUT /recurring/{id}'):
        rid = params.get('id') if route.startswith('PUT') else None
        payload = body_json(event)
        record = validate_rule(payload,rid)
        if not space_id or payload.get('paymentSource')=='personal':
            account_for(pk,record['accountId'],expense=record['type']=='expense')
        # Solo il modulo Nuovo movimento richiede la registrazione immediata.
        # Una ricorrenza creata dalla schermata Regole mantiene il vecchio comportamento.
        record_first = payload.get('recordFirstOccurrence', False)
        if type(record_first) is not bool:
            raise BadRequest('Opzione prima scadenza non valida')
        if rid and record_first:
            raise BadRequest('La prima scadenza è registrabile solo alla creazione')
        first_due = (not rid and record_first and record['active'] and
                     date_value(record['startDate']) <= dt.datetime.now(ZoneInfo('Europe/Rome')).date())
        first_tx = None
        if first_due:
            due = date_value(record['startDate'])
            txid = 'rec-'+record['id'][:45]+'-'+due.strftime('%Y%m%d')
            first_tx = {k:record[k] for k in ('description','type','category','note','amountCents','accountId')}
            first_tx.update({'id':txid,'date':due.isoformat(),'recurringId':record['id']})
        if space_id:
            source = payload.get('paymentSource','fund')
            if source not in ('fund','personal'):
                raise BadRequest('Origine pagamento non valida')
            record['paymentSource'] = source
            previous = get_row(data_pk,'REC#'+rid) if rid else None
            record['createdBy'] = ((previous or {}).get('value') or {}).get('createdBy',sub)
            actions = [auth_check(space_id,sub),put_action({'PK':data_pk,'SK':'REC#'+record['id'],'value':record},
                                            'attribute_exists(PK)' if rid else 'attribute_not_exists(PK)')]
            if first_tx:
                share_record(first_tx, record['paymentSource'])
                first_tx['createdBy'] = sub
                actions.append(put_action({'PK':data_pk,'SK':'TX#'+first_tx['id'],'value':first_tx}))
                mirror = mirrored_transaction(space_id,first_tx)
                actions.append(put_action({'PK':user_key(sub),'SK':'TX#'+mirror['id'],'value':mirror}))
            txn(actions)
            return response(200 if rid else 201,public(record))
        try:
            if first_tx:
                # Regola e primo movimento atomici: nessuna regola orfana se fallisce la creazione.
                txn(card_transaction_guard(pk,first_tx)+
                    [put_action({'PK':pk,'SK':'REC#'+record['id'],'value':record}),
                     put_action({'PK':pk,'SK':'TX#'+first_tx['id'],'value':first_tx})])
            else:
                TABLE.put_item(Item={'PK':pk,'SK':'REC#'+record['id'],'value':record},
                               ConditionExpression='attribute_exists(PK)' if rid else 'attribute_not_exists(PK)')
        except ClientError as err:
            if err.response['Error']['Code'] == 'ConditionalCheckFailedException':
                return response(404 if rid else 409,{'error':'Ricorrenza inesistente o duplicata'})
            raise
        return response(200 if rid else 201,public(record))
    if route == 'PUT /budgets/{category}':
        cat = params.get('category')
        if cat not in EXPENSE:
            raise BadRequest('Categoria budget non valida')
        amount = cents(body_json(event).get('amount'),allow_zero=True)
        if space_id:
            txn([auth_check(space_id,sub),put_action({'PK':data_pk,'SK':'B#'+cat,'amountCents':amount},None)])
        else:
            TABLE.put_item(Item={'PK':pk,'SK':'B#'+cat,'amountCents':amount})
        return response(200,{'category':cat,'amount':amount/100})
    return response(404,{'error':'Rotta inesistente'})


def handler(event, context):
    if event.get('source') == 'aws.scheduler' and event.get('operation') == 'recurring-tick':
        result=run_recurring()
        result['cardSettlements']=run_auto_card_settlements(dt.datetime.now(ZoneInfo('Europe/Rome')).date())
        return result
    claims = (((event.get('requestContext') or {}).get('authorizer') or {}).get('jwt') or {}).get('claims') or {}
    sub = claims.get('sub')
    if not isinstance(sub,str) or not sub or claims.get('token_use') != 'access' or claims.get('client_id') != CLIENT_ID:
        return response(403,{'error':'Access token Cognito richiesto'})
    try:
        return handle_request(event,claims,sub,user_key(sub),event.get('routeKey',''),event.get('pathParameters') or {})
    except BadRequest as err:
        return response(400,{'error':str(err)})
    except KeyError as err:
        return response(404,{'error':str(err).strip("'")})
    except PermissionError as err:
        return response(403,{'error':str(err)})
    except ClientError as err:
        code = err.response.get('Error',{}).get('Code')
        if code in ('ConditionalCheckFailedException','TransactionCanceledException'):
            return response(409,{'error':'Conflitto o permessi modificati; aggiorna la pagina e riprova'})
        LOG.exception('Errore AWS Moneta')
        return response(500,{'error':'Errore interno'})
    except Exception:
        LOG.exception('Errore interno Moneta')
        return response(500,{'error':'Errore interno'})
