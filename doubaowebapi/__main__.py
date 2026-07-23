"""Entry point: ``python -m doubaowebapi`` or ``doubaowebapi`` CLI."""

from .unified_server import run_server


def main():
    run_server()


if __name__ == "__main__":
    main()
