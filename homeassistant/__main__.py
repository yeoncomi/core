"""Start Home Assistant."""
from __future__ import annotations

import argparse
import faulthandler
import os
import sys
import threading

from .const import REQUIRED_PYTHON_VER, RESTART_EXIT_CODE, __version__

FAULT_LOG_FILENAME = "home-assistant.log.fault"


def validate_python() -> None:
    """Validate that the right Python version is running."""
    if sys.version_info[:3] < REQUIRED_PYTHON_VER:
        print(
            "Home Assistant requires at least Python "
            f"{REQUIRED_PYTHON_VER[0]}.{REQUIRED_PYTHON_VER[1]}.{REQUIRED_PYTHON_VER[2]}"
        )
        sys.exit(1)


def ensure_config_path(config_dir: str) -> None:
    """Validate the configuration directory."""
    # pylint: disable=import-outside-toplevel
    from . import config as config_util

    lib_dir = os.path.join(config_dir, "deps")

    # Test if configuration directory exists
    if not os.path.isdir(config_dir):
        if config_dir != config_util.get_default_config_dir():
            print(
                f"Fatal Error: Specified configuration directory {config_dir} "
                "does not exist"
            )
            sys.exit(1)

        try:
            os.mkdir(config_dir)
        except OSError:
            print(
                "Fatal Error: Unable to create default configuration "
                f"directory {config_dir}"
            )
            sys.exit(1)

    # Test if library directory exists
    if not os.path.isdir(lib_dir):
        try:
            os.mkdir(lib_dir)
        except OSError:
            print(f"Fatal Error: Unable to create library directory {lib_dir}")
            sys.exit(1)


def get_arguments() -> argparse.Namespace:
    """Get parsed passed in arguments."""
    # pylint: disable=import-outside-toplevel
    from . import config as config_util

    parser = argparse.ArgumentParser(
        description="Home Assistant: Observe, Control, Automate.",
        epilog=f"If restart is requested, exits with code {RESTART_EXIT_CODE}",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "-c",
        "--config",
        metavar="path_to_config_dir",
        default=config_util.get_default_config_dir(),
        help="Directory that contains the Home Assistant configuration",
    )
    parser.add_argument(
        "--safe-mode", action="store_true", help="Start Home Assistant in safe mode"
    )
    parser.add_argument(
        "--debug", action="store_true", help="Start Home Assistant in debug mode"
    )
    parser.add_argument(
        "--open-ui", action="store_true", help="Open the webinterface in a browser"
    )
    parser.add_argument(
        "--skip-pip",
        action="store_true",
        help="Skips pip install of required packages on startup",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable verbose logging to file."
    )
    parser.add_argument(
        "--pid-file",
        metavar="path_to_pid_file",
        default=None,
        help="Path to PID file useful for running as daemon",
    )
    parser.add_argument(
        "--log-rotate-days",
        type=int,
        default=None,
        help="Enables daily log rotation and keeps up to the specified days",
    )
    parser.add_argument(
        "--log-file",
        type=str,
        default=None,
        help="Log file to write to.  If not set, CONFIG/home-assistant.log is used",
    )
    parser.add_argument(
        "--log-no-color", action="store_true", help="Disable color logs"
    )
    parser.add_argument(
        "--script", nargs=argparse.REMAINDER, help="Run one of the embedded scripts"
    )
    if os.name == "posix":
        parser.add_argument(
            "--daemon", action="store_true", help="Run Home Assistant as daemon"
        )

    arguments = parser.parse_args()
    if os.name != "posix" or arguments.debug or arguments.runner:
        setattr(arguments, "daemon", False)

    return arguments


def daemonize() -> None:
    """Move current process to daemon process."""
    # Create first fork
    if os.fork() > 0:
        sys.exit(0)

    # Decouple fork
    os.setsid()

    # Create second fork
    if os.fork() > 0:
        sys.exit(0)

    # redirect standard file descriptors to devnull
    # pylint: disable=consider-using-with
    infd = open(os.devnull, encoding="utf8")
    outfd = open(os.devnull, "a+", encoding="utf8")
    sys.stdout.flush()
    sys.stderr.flush()
    os.dup2(infd.fileno(), sys.stdin.fileno())
    os.dup2(outfd.fileno(), sys.stdout.fileno())
    os.dup2(outfd.fileno(), sys.stderr.fileno())


def check_pid(pid_file: str) -> None:
    """Check that Home Assistant is not already running."""
    # Check pid file
    try:
        with open(pid_file, encoding="utf8") as file:
            pid = int(file.readline())
    except OSError:
        # PID File does not exist
        return

    # If we just restarted, we just found our own pidfile.
    if pid == os.getpid():
        return

    try:
        os.kill(pid, 0)
    except OSError:
        # PID does not exist
        return
    print("Fatal Error: Home Assistant is already running.")
    sys.exit(1)


def write_pid(pid_file: str) -> None:
    """Create a PID File."""
    pid = os.getpid()
    try:
        with open(pid_file, "w", encoding="utf8") as file:
            file.write(str(pid))
    except OSError:
        print(f"Fatal Error: Unable to write pid file {pid_file}")
        sys.exit(1)


def cmdline() -> list[str]:
    """Collect path and arguments to re-execute the current hass instance."""
    if os.path.basename(sys.argv[0]) == "__main__.py":
        modulepath = os.path.dirname(sys.argv[0])
        os.environ["PYTHONPATH"] = os.path.dirname(modulepath)
        return [sys.executable, "-m", "homeassistant"] + [
            arg for arg in sys.argv[1:] if arg != "--daemon"
        ]

    return [arg for arg in sys.argv if arg != "--daemon"]


def check_threads() -> None:
    """Check if there are any lingering threads."""
    try:
        nthreads = sum(
            thread.is_alive() and not thread.daemon for thread in threading.enumerate()
        )
        if nthreads > 1:
            sys.stderr.write(f"Found {nthreads} non-daemonic threads.\n")

    # Somehow we sometimes seem to trigger an assertion in the python threading
    # module. It seems we find threads that have no associated OS level thread
    # which are not marked as stopped at the python level.
    except AssertionError:
        sys.stderr.write("Failed to count non-daemonic threads.\n")


def main() -> int:
    """Start Home Assistant."""
    validate_python()

    args = get_arguments()

    if args.script is not None:
        # pylint: disable=import-outside-toplevel
        from . import scripts

        return scripts.run(args.script)

    config_dir = os.path.abspath(os.path.join(os.getcwd(), args.config))
    ensure_config_path(config_dir)

    # Daemon functions
    if args.pid_file:
        check_pid(args.pid_file)
    if args.daemon:
        daemonize()
    if args.pid_file:
        write_pid(args.pid_file)

    # pylint: disable=import-outside-toplevel
    from . import runner

    runtime_conf = runner.RuntimeConfig(
        config_dir=config_dir,
        verbose=args.verbose,
        log_rotate_days=args.log_rotate_days,
        log_file=args.log_file,
        log_no_color=args.log_no_color,
        skip_pip=args.skip_pip,
        safe_mode=args.safe_mode,
        debug=args.debug,
        open_ui=args.open_ui,
    )

    fault_file_name = os.path.join(config_dir, FAULT_LOG_FILENAME)
    with open(fault_file_name, mode="a", encoding="utf8") as fault_file:
        faulthandler.enable(fault_file)
        exit_code = runner.run(runtime_conf)
        faulthandler.disable()

    if os.path.getsize(fault_file_name) == 0:
        os.remove(fault_file_name)

    if exit_code == RESTART_EXIT_CODE:
        check_threads()

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
