# READ-ONLY checks against the PayPal SANDBOX using paypal-sandbox-credentials.xml (decrypted in memory only).
# Prints counts and pass/fail only. No credentials, tokens, transaction ids, payers or amounts are shown.
python -m roadworthy check-auth
python -m roadworthy txsearch --days 30
python -m roadworthy snapshot
$env:ROADWORTHY_LIVE = "1"
python -m pytest tests/test_txsearch.py -k LIVE -s
Remove-Item Env:ROADWORTHY_LIVE -ErrorAction SilentlyContinue
