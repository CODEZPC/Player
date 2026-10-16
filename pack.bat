RMDIR __pycache__ /s /q
py -m PyInstaller -D -w --clean -i .\_internal\Mp.ico main.py --add-data=".\_internal\;."
DEL main.spec
RMDIR build /s /q
pause
exit