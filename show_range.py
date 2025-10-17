p='c:\\Users\\Palle\\OneDrive\\Desktop\\reply to LinkedIn\\app.py'
with open(p,'r',encoding='utf-8') as f:
    lines=f.readlines()
for i in range(740, 835):
    if i-1 < len(lines):
        print(f"{i:4}: {lines[i-1].rstrip()}")
