#!/usr/bin/env python3
"""Seed demo Moneta in a SINGLE empty Cognito user's partition.

Default mode is read-only preview. Refuses any non-demo email, missing user,
existing data, or >100 transaction items. Does not create Cognito accounts.
Uses existing AWS resources only; uses a conditional DynamoDB transaction.
"""
import argparse
import calendar
import datetime as dt
import sys
from collections import defaultdict
from zoneinfo import ZoneInfo

DEMO_EMAIL='demo-moneta@gmail.com'
STACK='moneta-backend'
REGION='eu-south-1'


def move_month(date, shift):
    index=date.year*12+date.month-1+shift
    year, month0=divmod(index,12)
    month=month0+1
    return dt.date(year,month,min(date.day,calendar.monthrange(year,month)[1]))


def build_demo(sub, today=None):
    """Return a reproducible, fictitious dataset with a realistic spending mix."""
    today=today or dt.datetime.now(ZoneInfo('Europe/Rome')).date()
    first=today.replace(day=1)
    pk='USER#'+sub
    items=[]
    def put(sk, value=None, **extras):
        items.append({'PK':pk,'SK':sk,**({'value':value} if value is not None else {}),**extras})
    put('ACCOUNT#bank',{'id':'bank','name':'Conto corrente','kind':'bank','openingCents':260000})
    put('ACCOUNT#cash',{'id':'cash','name':'Contanti','kind':'cash','openingCents':9500})
    put('ACCOUNT#deposit',{'id':'deposit','name':'Conto deposito','kind':'deposit','openingCents':450000})
    card='cc-demo'
    put('ACCOUNT#'+card,{'id':card,'name':'Carta Demo','kind':'credit','openingCents':0,
        'closingDay':25,'chargeDay':10,'chargeMonthOffset':1,'settlementMode':'confirm'})
    monthly=[('income','Stipendio','Stipendio',215000,2),
        ('expense','Affitto casa','Affitto e mutuo',61000,3),
        ('expense','Spesa supermercato','Alimentari',24500,4),
        ('expense','Ristoranti e aperitivi','Ristoranti',11800,6),
        ('expense','Bollette utenze','Bollette',11500,8),
        ('expense','Rifornimenti','Carburante',9800,10),
        ('expense','Piattaforme streaming','Abbonamenti',3299,11),
        ('expense','Caffè e colazioni','Bar e caffè',6750,13),
        ('expense','Acquisti online','Shopping',12500,15),
        ('expense','Telefonia e fibra','Telefonia e Internet',3690,17),
        ('expense','Farmacia','Farmacia',2900,19),
        ('expense','Trasporto locale','Trasporti',4400,20),
        ('expense','Cinema e svago','Svago',6900,22),
        ('expense','Palestra','Salute',4500,24)]
    for offset in (-3,-2,-1):
        month=move_month(first,offset)
        for seq,(kind,name,cat,amount,day) in enumerate(monthly):
            due=month.replace(day=min(day,calendar.monthrange(month.year,month.month)[1]))
            txid=f'demo-{month:%Y%m}-{seq:02d}'
            put('TX#'+txid,{'id':txid,'description':name,'type':kind,'category':cat,
                           'amountCents':amount,'date':due.isoformat(),'accountId':'bank',
                           'note':'DEMO · operazione fittizia'})
        if offset==-2:
            txid=f'demo-{month:%Y%m}-extra'
            put('TX#'+txid,{'id':txid,'description':'Piccola vendita usato','type':'income',
                           'category':'Vendite','amountCents':14000,'date':month.replace(day=16).isoformat(),
                           'accountId':'bank','note':'DEMO · entrata fittizia'})
    # Current month: never invent future-paid spending.
    current=[('income','Stipendio dimostrativo','Stipendio',215000),
             ('expense','Spesa alimentare','Alimentari',4000)]
    for ix,(kind,name,cat,amount) in enumerate(current):
        txid=f'demo-{first:%Y%m}-current-{ix}'
        put('TX#'+txid,{'id':txid,'description':name,'type':kind,'category':cat,
                       'amountCents':amount,'date':today.isoformat(),
                       'accountId':'bank','note':'DEMO · operazione fittizia'})
    # Credit cycle checksum is necessary for safe later settlement.
    previous=move_month(first,-1)
    credit_day=previous.replace(day=12)
    txid=f'demo-credit-{previous:%Y%m}'
    put('TX#'+txid,{'id':txid,'description':'Cena con carta demo','type':'expense',
                   'category':'Ristoranti','amountCents':6900,'date':credit_day.isoformat(),
                   'accountId':card,'note':'DEMO · carta di credito fittizia'})
    put('CYCLE#'+card+'#'+previous.strftime('%Y-%m'),chargeCents=6900,revision=1)
    for seq,(origin,target,value,note) in enumerate((('bank','cash',12000,'Prelievo ATM demo'),
                                                     ('bank','deposit',45000,'Accantonamento demo'))):
        xid=f'demo-transfer-{seq}'
        put('XFER#'+xid,{'id':xid,'fromAccountId':origin,'toAccountId':target,
                         'amountCents':value,'date':move_month(first,-1).replace(day=14+seq).isoformat(),
                         'note':note})
    # Future-only recurring rule: avoid generating duplicate historical expenses.
    future=move_month(first,1).replace(day=8)
    rid='demo-rec-streaming'
    put('REC#'+rid,{'id':rid,'description':'Abbonamento musicale (demo)','type':'expense',
                    'category':'Abbonamenti','amountCents':1099,'note':'DEMO · ricorrenza fittizia',
                    'accountId':'bank','startDate':future.isoformat(),'frequency':'monthly','interval':1,
                    'endDate':None,'active':True})
    # Scheduled expense, not yet paid.
    due=move_month(first,1).replace(day=18)
    put('PLAN#demo-planned-1',{'id':'demo-planned-1','description':'Assicurazione auto (demo)',
        'category':'Assicurazioni','type':'expense','amountCents':18000,'date':due.isoformat(),
        'accountId':'bank','status':'scheduled','note':'DEMO · non ancora pagata'})
    for category,amount in [('Alimentari',27000),('Ristoranti',9000),('Shopping',9500),
                            ('Bar e caffè',4500),('Bollette',14000),('Abbonamenti',4000)]:
        put('B#'+category,amountCents=amount)
    put('DEMO#SEED_V1',{'email':DEMO_EMAIL,'createdAt':today.isoformat(),
                        'description':'Dati sintetici, non attribuibili a persone reali'})
    if len(items)>100:
        raise ValueError('Troppe righe per una transazione DynamoDB atomica')
    return items


def discover(cfn):
    values={o['OutputKey']:o['OutputValue'] for o in cfn.describe_stacks(StackName=STACK)['Stacks'][0].get('Outputs',[])}
    if not values.get('UserPoolId') or not values.get('TableName'):
        raise RuntimeError('Stack Moneta privo degli output UserPoolId o TableName')
    return values['UserPoolId'],values['TableName']


def main(argv=None):
    ap=argparse.ArgumentParser(description='Dati FINTIZI solo per demo-moneta@gmail.com (preview per default)')
    ap.add_argument('--execute',action='store_true',help='Esegue realmente la scrittura')
    ap.add_argument('--confirm-email',default='',help='Conferma esplicita dell’email demo')
    args=ap.parse_args(argv)
    if args.execute and args.confirm_email != DEMO_EMAIL:
        ap.error('Per eseguire serve --confirm-email '+DEMO_EMAIL)
    import boto3
    from boto3.dynamodb.types import TypeSerializer
    session=boto3.session.Session(region_name=REGION)
    pool,table=discover(session.client('cloudformation'))
    cognito=session.client('cognito-idp')
    found=cognito.list_users(UserPoolId=pool,Filter=f'email = "{DEMO_EMAIL}"',Limit=10)['Users']
    found=[u for u in found if next((x['Value'] for x in u.get('Attributes',[]) if x['Name']=='email'),None)==DEMO_EMAIL]
    if len(found)!=1:
        raise RuntimeError('Deve esistere esattamente un account Cognito con email '+DEMO_EMAIL)
    target=found[0]
    sub=next((x['Value'] for x in target['Attributes'] if x['Name']=='sub'),None)
    if not sub or not target.get('Enabled',False):
        raise RuntimeError('Account demo non attivo o privo di sub Cognito')
    db=session.resource('dynamodb').Table(table)
    from boto3.dynamodb.conditions import Key
    existing=db.query(KeyConditionExpression=Key('PK').eq('USER#'+sub),Limit=1).get('Items',[])
    if existing:
        raise RuntimeError('Archivio dell’account demo NON VUOTO: operazione annullata per proteggere i dati')
    items=build_demo(sub)
    print(f'Account di destinazione: {DEMO_EMAIL} (sub letto dal pool Cognito, non mostrato)')
    print(f'Tabella: {table}; regione: {REGION}; record fittizi: {len(items)}')
    kinds=defaultdict(int)
    for item in items:kinds[item['SK'].split('#')[0]]+=1
    print('Anteprima record:',', '.join(f'{k}: {v}' for k,v in sorted(kinds.items())))
    if not args.execute:
        print('PREVIEW: nessuna scrittura. Dopo aver controllato esegui con --execute --confirm-email '+DEMO_EMAIL)
        return 0
    serializer=TypeSerializer()
    api=session.client('dynamodb')
    request=[{'Put':{'TableName':table,'Item':{k:serializer.serialize(v) for k,v in row.items()},
                     'ConditionExpression':'attribute_not_exists(PK)'}} for row in items]
    api.transact_write_items(TransactItems=request)
    print('IMPORT COMPLETATO: dati fittizi scritti soltanto nella partizione Cognito demo.')
    return 0

if __name__=='__main__':
    try:sys.exit(main())
    except Exception as exc:
        print('OPERAZIONE NON COMPLETATA:',exc,file=sys.stderr)
        sys.exit(1)
