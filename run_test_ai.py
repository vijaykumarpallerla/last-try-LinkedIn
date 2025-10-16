import os, traceback
os.chdir(r"C:\Users\Palle\OneDrive\Desktop\All Done LinkedIn")
try:
    from app import app
    c = app.test_client()
    resp = c.get('/test-ai')
    print('STATUS', resp.status_code)
    print(resp.get_data(as_text=True))
except Exception as e:
    print('ERROR', e)
    traceback.print_exc()
