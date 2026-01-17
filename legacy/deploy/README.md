```
# First time setup
cd deploy
uv venv
source .venv/bin/activate
uv pip install -e .

# Subsequent usage
python deploy/models/omniparser/deploy.py start
```
