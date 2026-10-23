# agentic-football

Rule-based player agent for AWS Agentic Football Cup (Amazon Bedrock AgentCore Runtime).

## Local test (Windows PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt bedrock-agentcore-starter-toolkit
python agent.py
# second terminal:
Invoke-RestMethod -Uri http://localhost:8080/invocations -Method Post -ContentType "application/json" -InFile .\sample_payload.json | ConvertTo-Json -Depth 5
```
