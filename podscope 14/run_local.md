# Run locally in 30 seconds

```bash
pip install -r requirements.txt
python scripts_test_local.py        # seeds synthetic data + runs all checks
uvicorn app.api.main:app --reload   # then open http://127.0.0.1:8000
```

Visit `/show/the-daily-1200361736` to see a populated show page.

# First real collection (no seed data)

```bash
python -m app.collectors.run        # hits live Apple + Spotify feeds
```
Then the same show pages fill with real chart data.
