"""PyInstaller entry point — uses absolute import to avoid relative import issues."""
from socket_server.cli import main

if __name__ == '__main__':
    main()
