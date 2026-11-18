import re
content = open('routers/editor_router.py').read()
old = 'async def editor_login(request: Request):'
new = 'async def editor_login(request: Request):\n    import sys; print("DEBUG: KIT_EDITOR_PASSWORD =", repr(KIT_EDITOR_PASSWORD), file=sys.stderr, flush=True)'
assert old in content
content = content.replace(old, new, 1)
open('routers/editor_router.py', 'w').write(content)
print('patched')
