import os, json, re, boto3, datetime as dt

sns = boto3.client('sns')
ddb = boto3.resource('dynamodb').Table(os.environ['TABLE_NAME'])

TOPIC_ARN   = os.environ['TOPIC_ARN']
MAX_SUBS    = int(os.environ.get('MAX_SUBS', '7'))
CORS_ORIGIN = os.environ.get('CORS_ORIGIN', '*')

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

def _cors():
    return {
        "Access-Control-Allow-Origin": CORS_ORIGIN,
        "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type"
    }

def _json(status, body):
    return {"statusCode": status, "headers": _cors(), "body": json.dumps(body, ensure_ascii=False)}

def _count_current():
    resp = ddb.scan(ProjectionExpression="email")
    return len(resp.get('Items', []))

def _list_sns():
    out, token = {}, None
    while True:
        resp = sns.list_subscriptions_by_topic(TopicArn=TOPIC_ARN, NextToken=token) if token else sns.list_subscriptions_by_topic(TopicArn=TOPIC_ARN)
        for s in resp.get('Subscriptions', []):
            ep = (s.get('Endpoint') or "").lower()
            arn = s.get('SubscriptionArn') or ""
            if not ep: 
                continue
            status = 'PENDING' if arn == 'PendingConfirmation' else ('CONFIRMED' if arn else 'UNKNOWN')
            out[ep] = {"arn": arn, "status": status}
        token = resp.get('NextToken')
        if not token: break
    return out

def _sync_ddb_with_sns():
    sns_map = _list_sns()
    resp = ddb.scan()
    for it in resp.get('Items', []):
        em = it['email'].lower()
        s = sns_map.get(em)
        if s and it.get('status') != s['status']:
            ddb.update_item(
                Key={"email": em},
                UpdateExpression="SET #s = :s, subscription_arn = :a",
                ExpressionAttributeNames={"#s":"status"},
                ExpressionAttributeValues={":s": s['status'], ":a": s['arn']}
            )

def handler(event, context):
    # Soporta HTTP API (v2)
    http = event.get('requestContext', {}).get('http', {})
    method = http.get('method', 'GET')
    path   = event.get('rawPath', '/')

    if method == 'OPTIONS':
        return {"statusCode": 200, "headers": _cors(), "body": ""}

    if path == '/subscribe' and method == 'POST':
        body = json.loads(event.get('body') or "{}")
        email = (body.get('email') or "").strip().lower()
        if not EMAIL_RE.match(email):
            return _json(400, {"error": "Email inválido"})
        if _count_current() >= MAX_SUBS:
            return _json(409, {"error": f"Límite de {MAX_SUBS} suscriptores alcanzado"})
        # ¿Ya existe?
        existing = ddb.get_item(Key={"email": email}).get('Item')
        if existing:
            return _json(200, {"message": f"{email} ya estaba {existing.get('status','UNKNOWN')}"})
        # Suscribe en SNS (envía email de confirmación)
        sns.subscribe(TopicArn=TOPIC_ARN, Protocol='email', Endpoint=email, ReturnSubscriptionArn=False)
        ddb.put_item(Item={"email": email, "status": "PENDING", "created_at": dt.datetime.utcnow().isoformat()})
        return _json(200, {"message": f"Enviamos un correo de confirmación a {email}. Debe aceptarlo."})

    if path == '/unsubscribe' and method == 'POST':
        body = json.loads(event.get('body') or "{}")
        email = (body.get('email') or "").strip().lower()
        if not EMAIL_RE.match(email):
            return _json(400, {"error": "Email inválido"})

        arn = None
        it = ddb.get_item(Key={"email": email}).get('Item')
        if it: arn = it.get('subscription_arn')

        if not arn or arn == 'PendingConfirmation':
            # Buscar en SNS por si cambió fuera del sistema
            s = _list_sns().get(email)
            if s and s['arn'] not in ('', None, 'PendingConfirmation'):
                arn = s['arn']

        if arn and arn != 'PendingConfirmation':
            sns.unsubscribe(SubscriptionArn=arn)

        ddb.delete_item(Key={"email": email})
        return _json(200, {"message": f"{email} desuscripto (si estaba confirmado, ya no recibirá correos)."})

    if path == '/subscribers' and method == 'GET':
        _sync_ddb_with_sns()
        resp = ddb.scan(ProjectionExpression="email, #s", ExpressionAttributeNames={"#s":"status"})
        items = sorted(resp.get('Items', []), key=lambda x: x['email'])
        return _json(200, {"subscribers": items})

    return _json(404, {"error": "Ruta no encontrada"})
