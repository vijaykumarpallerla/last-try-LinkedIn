import ast
p='c:\\Users\\Palle\\OneDrive\\Desktop\\reply to LinkedIn\\app.py'
try:
    with open(p,'r',encoding='utf-8') as f:
        src=f.read()
    ast.parse(src)
    print('OK')
except SyntaxError as e:
    print('SyntaxError:',e.msg)
    print('Line:',e.lineno,'Offset:',e.offset)
    lines=src.splitlines()
    start=max(0,e.lineno-5)
    for i in range(start, min(len(lines), e.lineno+2)):
        ln=i+1
        mark = '->' if ln==e.lineno else '  '
        print(f"{mark} {ln:4}: {lines[i]!r}")
