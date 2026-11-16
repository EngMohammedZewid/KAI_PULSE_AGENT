import os, urllib.request, urllib.parse, json, sys

API_KEY = os.environ["WXO_API_KEY"]
INSTANCE_ID = os.environ["WXO_INSTANCE_ID"]
BASE = f"https://api.us-south.watson-orchestrate.cloud.ibm.com/instances/{INSTANCE_ID}"

# 1. Get IAM token
data = urllib.parse.urlencode({
    "grant_type": "urn:ibm:params:oauth:grant-type:apikey",
    "apikey": API_KEY,
}).encode()
req = urllib.request.Request(
    "https://iam.cloud.ibm.com/identity/token",
    data=data,
    headers={"Content-Type": "application/x-www-form-urlencoded"},
)
with urllib.request.urlopen(req) as r:
    token = json.loads(r.read())["access_token"]
print("IAM token OK")

headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

# 2. Try several API paths to find agents and CRN
paths = [
    "/v1/agents",
    "/v2/agents",
    "/v1/wx_agents",
    "/v1/instance",
    "/v1/settings",
    "/v1/info",
]

for path in paths:
    try:
        req2 = urllib.request.Request(BASE + path, headers=headers)
        with urllib.request.urlopen(req2) as r:
            body = json.loads(r.read())
        print(f"\n=== {path} (200) ===")
        print(json.dumps(body, indent=2)[:3000])
    except urllib.error.HTTPError as e:
        print(f"{path}: HTTP {e.code}")
    except Exception as e:
        print(f"{path}: {e}")
