import app
client = app.app.test_client()
with client.session_transaction() as sess:
    sess['user_id'] = 1
    sess['role'] = 'user'
    sess['username'] = 'Test'
resp = client.get('/profile')
print(resp.status_code)
print(resp.text[:500])
