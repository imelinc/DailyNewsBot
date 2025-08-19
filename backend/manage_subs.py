# manage_subs.py
import os
import json
import re
import boto3
import datetime as dt

sns = boto3.client('sns')
ddb = boto3.resource('dynamodb').Table(os.environ['TABLE_NAME'])

TOPIC_ARN   = os.environ['TOPIC_ARN']
MAX_SUBS    = int(os.environ.get('MAX_SUBS', '7'))
CORS_ORIGIN = os.environ.get('CORS_ORIGIN', '*')

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# -------------------- helpers --------------------

def _cors():
    return {
        "Access-Control-Allow-Origin": CORS_ORIGIN,
        "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type"
    }

def _json(status, body):
    return {
        "statusCode": status,
        "headers": _cors(),
        "body": json.dumps(body, ensure_ascii=False)
    }

def _is_valid_arn(arn: str) -> bool:
    return isinstance(arn, str) and arn.startswith("arn:aws:sns:") and arn.count(":") >= 5

def _count_current():
    # Cuenta suscriptores (pendientes + confirmados)
    resp = ddb.scan(ProjectionExpression="email")
    return len(resp.get('Items', []))

def _list_sns():
    # Devuelve { email_lower: {arn, status} }
    out, token = {}, None
    while True:
        resp = sns.list_subscriptions_by_topic(
            TopicArn=TOPIC_ARN,
            NextToken=token
        ) if token else sns.list_subscriptions_by_topic(TopicArn=TOPIC_ARN)

        for s in resp.get('Subscriptions', []):
            ep = (s.get('Endpoint') or "").strip().lower()
            arn = (s.get('SubscriptionArn') or "").strip()
            if not ep:
                continue
            status = 'PENDING' if arn == 'PendingConfirmation' else ('CONFIRMED' if arn else 'UNKNOWN')
            out[ep] = {"arn": arn, "status": status}

        token = resp.get('NextToken')
        if not token:
            break
    return out

def _sync_ddb_with_sns():
    # Escribe/actualiza status y ARN en DDB según lo que ve SNS
    sns_map = _list_sns()
    resp = ddb.scan()
    for it in resp.get('Items', []):
        em = it['email'].lower()
        info = sns_map.get(em)
        if info and (it.get('status') != info['status'] or it.get('subscription_arn') != info['arn']):
            ddb.update_item(
                Key={"email": em},
                UpdateExpression="SET #s = :s, subscription_arn = :a",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":s": info['status'], ":a": info['arn']}
            )

# -------------------- handler --------------------

def handler(event, context):
    # Soporte para HTTP API (payload v2)
    http = event.get('requestContext', {}).get('http', {})
    method = http.get('method', 'GET')
    path   = event.get('rawPath', '/')

    if method == 'OPTIONS':
        # Preflight CORS
        return {"statusCode": 200, "headers": _cors(), "body": ""}

    try:
        # ---------- POST /subscribe ----------
        if path == '/subscribe' and method == 'POST':
            body = json.loads(event.get('body') or "{}")
            email = (body.get('email') or "").strip().lower()

            if not EMAIL_RE.match(email):
                return _json(400, {"error": "Email inválido"})

            # límite 7
            if _count_current() >= MAX_SUBS:
                return _json(409, {"error": f"Límite de {MAX_SUBS} suscriptores alcanzado"})

            # ya existe?
            existing = ddb.get_item(Key={"email": email}).get('Item')
            if existing:
                return _json(200, {"message": f"{email} ya estaba {existing.get('status','UNKNOWN')}"})

            # Suscribe en SNS (envía email de confirmación)
            sns.subscribe(
                TopicArn=TOPIC_ARN,
                Protocol='email',
                Endpoint=email,
                ReturnSubscriptionArn=False
            )

            # Guarda en DDB como PENDING
            ddb.put_item(Item={
                "email": email,
                "status": "PENDING",
                "created_at": dt.datetime.utcnow().isoformat()
            })

            return _json(200, {"message": f"Enviamos un correo de confirmación a {email}. Debe aceptarlo."})

        # ---------- POST /unsubscribe ----------
        if path == '/unsubscribe' and method == 'POST':
            body = json.loads(event.get('body') or "{}")
            email = (body.get('email') or "").strip().lower()

            if not EMAIL_RE.match(email):
                return _json(400, {"error": "Email inválido"})

            # 1) intentar obtener ARN desde DDB
            arn = None
            item = ddb.get_item(Key={"email": email}).get('Item')
            if item:
                arn = item.get('subscription_arn')

            # 2) si no es un ARN válido, buscar en SNS por endpoint
            if not _is_valid_arn(arn):
                info = _list_sns().get(email)
                if info:
                    arn = info.get('arn')

            # 3) casos especiales
            if arn == 'PendingConfirmation':
                # No existe todavía un ARN "real" en SNS
                ddb.delete_item(Key={"email": email})
                return _json(200, {"message": f"{email} estaba pendiente de confirmación. Se eliminó del listado local."})

            if not _is_valid_arn(arn):
                # No hay suscripción activa en SNS
                ddb.delete_item(Key={"email": email})
                return _json(200, {"message": f"No encontramos suscripción SNS activa para {email}. Se eliminó del listado."})

            # 4) desuscribir en SNS + borrar de DDB
            sns.unsubscribe(SubscriptionArn=arn)
            ddb.delete_item(Key={"email": email})
            return _json(200, {"message": f"{email} desuscripto correctamente."})

        # ---------- GET /subscribers ----------
        if path == '/subscribers' and method == 'GET':
            _sync_ddb_with_sns()
            resp = ddb.scan(ProjectionExpression="email, #s", ExpressionAttributeNames={"#s": "status"})
            items = sorted(resp.get('Items', []), key=lambda x: x['email'])
            return _json(200, {"subscribers": items})

        # ---------- default ----------
        return _json(404, {"error": "Ruta no encontrada"})

    except Exception as e:
        # Falla controlada para no romper API Gateway
        print("ERROR:", repr(e))
        return _json(500, {"error": "Unexpected error", "detail": str(e)})
