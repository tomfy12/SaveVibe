import app
client = app.app.test_client()
with client.session_transaction() as sess:
    sess['user_id'] = 1
    sess['role'] = 'admin'
    sess['username'] = 'Admin'
resp = client.get('/profile')
print(resp.status_code)
