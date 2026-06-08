"""
Bedrock Chat - Login via Identity Center and chat with LLaMA.
Caches credentials locally (~/.bedrock_creds.json). Re-login needed every ~8h (session) or 15 days (token).
Usage: python test_bedrock.py [prompt]
"""
import json, sys, time, webbrowser, os, boto3
from datetime import datetime, timezone

SSO_START_URL = "https://d-90663e488b.awsapps.com/start"
REGION = "us-east-1"
ACCOUNT_ID = "248189947068"
ROLE_NAME = "BedrockFullAccess"
MODEL_ID = "us.meta.llama4-scout-17b-instruct-v1:0"
CACHE_FILE = os.path.expanduser("~/.bedrock_creds.json")


def save_creds(creds):
    with open(CACHE_FILE, "w") as f:
        json.dump(creds, f)
    os.chmod(CACHE_FILE, 0o600)


def load_creds():
    if not os.path.exists(CACHE_FILE):
        return None
    with open(CACHE_FILE) as f:
        creds = json.load(f)
    if creds["expiration"] < int(datetime.now(timezone.utc).timestamp() * 1000):
        return None
    return creds


def sso_login():
    sso_oidc = boto3.client("sso-oidc", region_name=REGION)
    client = sso_oidc.register_client(clientName="bedrock-chat", clientType="public")
    auth = sso_oidc.start_device_authorization(
        clientId=client["clientId"], clientSecret=client["clientSecret"], startUrl=SSO_START_URL
    )
    print(f"\n🔐 Opening browser for login...\n   If it doesn't open: {auth['verificationUriComplete']}\n")
    webbrowser.open(auth["verificationUriComplete"])

    token = None
    while not token:
        time.sleep(auth["interval"])
        try:
            token = sso_oidc.create_token(
                clientId=client["clientId"], clientSecret=client["clientSecret"],
                grantType="urn:ietf:params:oauth:grant-type:device_code", deviceCode=auth["deviceCode"],
            )
        except sso_oidc.exceptions.AuthorizationPendingException:
            pass

    sso = boto3.client("sso", region_name=REGION)
    creds = sso.get_role_credentials(
        roleName=ROLE_NAME, accountId=ACCOUNT_ID, accessToken=token["accessToken"]
    )["roleCredentials"]
    save_creds(creds)
    return creds


def get_client():
    creds = load_creds()
    if not creds:
        print("🔑 Credentials expired or not found. Logging in...")
        creds = sso_login()
    else:
        print("✓ Using cached credentials.")
    return boto3.client(
        "bedrock-runtime", region_name=REGION,
        aws_access_key_id=creds["accessKeyId"],
        aws_secret_access_key=creds["secretAccessKey"],
        aws_session_token=creds["sessionToken"],
    )


def chat(client, prompt):
    response = client.invoke_model(
        modelId=MODEL_ID,
        body=json.dumps({"prompt": f"<|begin_of_text|><|start_header_id|>user<|end_header_id|>\n{prompt}\n<|start_header_id|>assistant<|end_header_id|>\n", "max_gen_len": 512, "temperature": 0.5}),
        contentType="application/json",
    )
    return json.loads(response["body"].read())["generation"]


if __name__ == "__main__":
    client = get_client()
    # One-shot mode if prompt passed as argument
    if len(sys.argv) > 1:
        print(chat(client, " ".join(sys.argv[1:])))
    else:
        print("✓ Ready! Type 'exit' to quit.\n")
        while True:
            prompt = input("You: ").strip()
            if not prompt or prompt.lower() == "exit":
                break
            try:
                print(f"\n{chat(client, prompt)}\n")
            except Exception:
                print("🔑 Session expired. Re-authenticating...")
                client = get_client()
                print(f"\n{chat(client, prompt)}\n")
