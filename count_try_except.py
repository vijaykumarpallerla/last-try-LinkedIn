p='c:\\Users\\Palle\\OneDrive\\Desktop\\reply to LinkedIn\\app.py'
with open(p,'r',encoding='utf-8') as f:
    lines=f.readlines()
up_to=812
try_count=0
except_count=0
for i,l in enumerate(lines[:up_to]):
    s=l.strip()
    if s.startswith('try:'):
        try_count+=1
    if s.startswith('except'):
        except_count+=1
print('try_count',try_count,'except_count',except_count)
for i in range(800,820):
    if i-1 < len(lines):
        print(i,repr(lines[i-1]))
