"""Entry point for snappy."""

from snappy.app import SnappyApp


def main() -> None:
    app = SnappyApp()
    app.run()


if __name__ == "__main__":
    main()
