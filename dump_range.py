p='c:\\Users\\Palle\\OneDrive\\Desktop\\reply to LinkedIn\\app.py'
with open(p,'rb') as f:
    lines=f.readlines()
for idx in range(800,817):
    if idx-1 < len(lines):
        print(idx, repr(lines[idx-1]))
