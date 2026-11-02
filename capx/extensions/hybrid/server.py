"""Legacy launch entry point; HTTP serving and orchestration live in Relay."""
from relay.server import (
    Console,
    TestGenerator,
    TestVla,
    restore_recorded_route,
    restore_recorded_session,
)
from relay.server import main as _main


def main():
    return _main(default_backend="libero", default_code_executor="capx")


if __name__ == "__main__":
    main()
