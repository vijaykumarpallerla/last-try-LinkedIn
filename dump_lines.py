p='c:\\Users\\Palle\\OneDrive\\Desktop\\reply to LinkedIn\\app.py'
with open(p,'rb') as f:
    for i in range(1,61):
        line=f.readline()
        if not line:
            break
        print(f"{i:03}: {line!r}")
