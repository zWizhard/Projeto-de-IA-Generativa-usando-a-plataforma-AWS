"""
Authenticate via IAM Identity Center and get temporary credentials for Amazon Bedrock.

Usage:
    python get_bedrock_creds.py

First run will open a browser for login. Subsequent runs reuse the cached token until it expires.
"""
import json
import time
import webbrowser
import boto3

SSO_START_URL = "https://d-90663e488b.awsapps.com/start"
SSO_REGION = "us-east-1"
ACCOUNT_ID = "248189947068"
ROLE_NAME = "BedrockFullAccess"

sso_oidc = boto3.client("sso-oidc", region_name=SSO_REGION)

# Register client
client = sso_oidc.register_client(clientName="bedrock-login", clientType="public")

# Start device authorization
auth = sso_oidc.start_device_authorization(
    clientId=client["clientId"],
    clientSecret=client["clientSecret"],
    startUrl=SSO_START_URL,
)

print(f"\nOpening browser for login...\nIf it doesn't open, go to: {auth['verificationUriComplete']}\n")
webbrowser.open(auth["verificationUriComplete"])

# Poll for token
token = None
while not token:
    time.sleep(auth["interval"])
    try:
        token = sso_oidc.create_token(
            clientId=client["clientId"],
            clientSecret=client["clientSecret"],
            grantType="urn:ietf:params:oauth:grant-type:device_code",
            deviceCode=auth["deviceCode"],
        )
    except sso_oidc.exceptions.AuthorizationPendingException:
        pass

# Get role credentials
sso = boto3.client("sso", region_name=SSO_REGION)

# List available accounts/roles for the user
accounts = sso.list_accounts(accessToken=token["accessToken"])["accountList"]
if not accounts:
    print("✗ No accounts available. Ask your admin to assign a permission set to your group.")
    exit(1)

print("Available accounts/roles:")
for acc in accounts:
    roles = sso.list_account_roles(accessToken=token["accessToken"], accountId=acc["accountId"])["roleList"]
    for role in roles:
        print(f"  {acc['accountId']} - {role['roleName']}")

creds = sso.get_role_credentials(
    roleName=ROLE_NAME,
    accountId=ACCOUNT_ID,
    accessToken=token["accessToken"],
)["roleCredentials"]

print("✓ Authenticated!\n")
print("Export these or use them in your code:\n")
print(f"export AWS_ACCESS_KEY_ID={creds['accessKeyId']}")
print(f"export AWS_SECRET_ACCESS_KEY={creds['secretAccessKey']}")
print(f"export AWS_SESSION_TOKEN={creds['sessionToken']}")

# Quick test
print("\n--- Testing Bedrock access ---")
bedrock = boto3.client(
    "bedrock",
    region_name=SSO_REGION,
    aws_access_key_id=creds["accessKeyId"],
    aws_secret_access_key=creds["secretAccessKey"],
    aws_session_token=creds["sessionToken"],
)
models = bedrock.list_foundation_models(byProvider="Anthropic")
print(f"✓ Bedrock accessible. Found {len(models['modelSummaries'])} Anthropic models.")
