<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Connecting your IDE to the Tokenomics gateway

The Tokenomics gateway is an **OpenAI-compatible endpoint**. Any coding IDE or agent that lets
you set a custom OpenAI base URL + API key can route its model traffic through it. You always
request a single **virtual model**; the gateway resolves the concrete model based on your current
budget tier and enforces your daily budget (a hard 403 stop at 100%).

## What you need

| Setting | Value |
| --- | --- |
| **Base URL** | `https://<gateway-id>.gateway.bedrock-agentcore.<region>.amazonaws.com/inference/v1` |
| **Model** | `team-coding-model` (the virtual model; the gateway picks the real one) |
| **API key** | Your Cognito **access token** |

Get all three from the **IDE Setup** tab of the developer portal (it shows the exact base URL,
the virtual model, and a **Copy my token** button). The token is short-lived — re-copy it after
signing in again when it expires.

> The API key is your **access token**, not the ID token. The portal's *Copy my token* button
> already gives you the access token. If you mint tokens yourself, use the `AccessToken` from the
> Cognito auth result.

## Why a virtual model?

You never hard-code `gpt-5.5` or `claude-opus` in your IDE. You always ask for
`team-coding-model`. As your team's daily spend rises past the configured thresholds, the gateway
automatically routes you down the tier ladder (premium -> mid -> open-source), and blocks new
requests once the budget is exhausted. Your IDE config never changes.

## Claude Code

Claude Code speaks the OpenAI-compatible protocol when pointed at a custom base URL. Set:

```bash
export OPENAI_BASE_URL="https://<gateway-id>.gateway.bedrock-agentcore.<region>.amazonaws.com/inference/v1"
export OPENAI_API_KEY="<your-access-token>"
export OPENAI_MODEL="team-coding-model"
```

Then start Claude Code pointed at the OpenAI-compatible provider. Requests are metered under your
own developer project.

## Cursor

Settings -> Models -> **OpenAI API Key** / **Override OpenAI Base URL**:

- Base URL: `https://<gateway-id>.gateway.bedrock-agentcore.<region>.amazonaws.com/inference/v1`
- API key: your access token
- Add a custom model named `team-coding-model` and select it.

## Continue (VS Code / JetBrains)

In `~/.continue/config.json`, add an OpenAI-compatible model provider:

```json
{
  "models": [
    {
      "title": "Tokenomics",
      "provider": "openai",
      "model": "team-coding-model",
      "apiBase": "https://<gateway-id>.gateway.bedrock-agentcore.<region>.amazonaws.com/inference/v1",
      "apiKey": "<your-access-token>"
    }
  ]
}
```

## Any OpenAI SDK client

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://<gateway-id>.gateway.bedrock-agentcore.<region>.amazonaws.com/inference/v1",
    api_key="<your-access-token>",
)

resp = client.chat.completions.create(
    model="team-coding-model",
    messages=[{"role": "user", "content": "Write a one-line hello world in Python."}],
)
print(resp.choices[0].message.content)
print("served by:", resp.model)   # the concrete model the gateway routed to
print("usage:", resp.usage)       # metered under your developer project
```

```bash
curl -s -X POST \
  "https://<gateway-id>.gateway.bedrock-agentcore.<region>.amazonaws.com/inference/v1/chat/completions" \
  -H "Authorization: Bearer <your-access-token>" \
  -H "Content-Type: application/json" \
  -d '{"model":"team-coding-model","messages":[{"role":"user","content":"hi"}]}'
```

## What you'll see

- **`model` in the response** is the concrete model the gateway routed to for your current tier
  (e.g. `mistral.mistral-large-3-675b-instruct`), not `team-coding-model`.
- **`usage`** reports real token counts; these are metered under **your own developer project** so
  your spend is attributed to you and rolls up to your customer project / cost center.
- **HTTP 403** with a policy message means you've hit 100% of your daily budget — the gateway's
  Cedar policy denied the request. It clears when the budget resets or an admin raises it.

## How auth works (short version)

You authenticate with your own Cognito credentials in the portal (username + password, no
redirect). The gateway accepts the resulting in-app access token as the outer gate; the real
per-developer authorization happens inside the gateway: a request interceptor maps your token to
your project/budget/tier (fail-closed for anyone unmapped) and the Cedar policy blocks over-budget
requests. See [`SECURITY_ACCEPTED_RISKS.md`](./SECURITY_ACCEPTED_RISKS.md) for the details and the
production hardening alternative (Hosted UI authorization-code flow for a scoped token).
