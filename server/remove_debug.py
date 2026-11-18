content = open('routers/editor_router.py').read()
old = '    import sys; print("DEBUG: KIT_EDITOR_PASSWORD =", repr(KIT_EDITOR_PASSWORD), file=sys.stderr, flush=True)\n'
content = content.replace(old, '')
open('routers/editor_router.py', 'w').write(content)
print('DEBUG' in content)
"