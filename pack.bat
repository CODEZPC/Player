RMDIR __pycache__ /s /q
pyinstaller -D -w --clean -i .\_internal\Mp.ico main.py --add-data ".\_internal\MP.ico;."
DEL main.spec
RMDIR build /s /q