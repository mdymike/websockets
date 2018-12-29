import argparse
import asyncio
import os
import signal
import sys
import threading

import websockets
from websockets.exceptions import format_close


def win_enable_vt100():
    """
    Enable VT-100 for console output on Windows.

    See also https://bugs.python.org/issue29059.

    """
    import ctypes

    STD_OUTPUT_HANDLE = ctypes.c_uint(-11)
    INVALID_HANDLE_VALUE = ctypes.c_uint(-1)
    ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x004

    handle = ctypes.windll.kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
    if handle == INVALID_HANDLE_VALUE:
        raise RuntimeError("Unable to obtain stdout handle")

    cur_mode = ctypes.c_uint()
    if ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(cur_mode)) == 0:
        raise RuntimeError("Unable to query current console mode")

    # ctypes ints lack support for the required bit-OR operation.
    # Temporarily convert to Py int, do the OR and convert back.
    py_int_mode = int.from_bytes(cur_mode, sys.byteorder)
    new_mode = ctypes.c_uint(py_int_mode | ENABLE_VIRTUAL_TERMINAL_PROCESSING)

    if ctypes.windll.kernel32.SetConsoleMode(handle, new_mode) == 0:
        raise RuntimeError("Unable to set console mode")


def exit_from_event_loop_thread(loop, stop):
    loop.stop()
    if not stop.done():
        # When exiting the thread that runs the event loop, raise
        # KeyboardInterrupt in the main thead to exit the program.
        try:
            ctrl_c = signal.CTRL_C_EVENT  # Windows
        except AttributeError:
            ctrl_c = signal.SIGINT  # POSIX
        os.kill(os.getpid(), ctrl_c)


def print_during_input(string):
    sys.stdout.write(
        (
            # Save cursor position
            "\N{ESC}7"
            # Add a new line
            "\N{LINE FEED}"
            # Move cursor up
            "\N{ESC}[A"
            # Insert blank line, scroll last line down
            "\N{ESC}[L"
            # Print string in the inserted blank line
            "{string}\N{LINE FEED}"
            # Restore cursor position
            "\N{ESC}8"
            # Move cursor down
            "\N{ESC}[B"
        ).format(string=string)
    )
    sys.stdout.flush()


def print_over_input(string):
    sys.stdout.write(
        (
            # Move cursor to beginning of line
            "\N{CARRIAGE RETURN}"
            # Delete current line
            "\N{ESC}[K"
            # Print string
            "{string}\N{LINE FEED}"
        ).format(string=string)
    )
    sys.stdout.flush()


async def run_client(uri, loop, inputs, stop):
    try:
        websocket = await websockets.connect(uri)
    except Exception as exc:
        print_over_input("Failed to connect to {}: {}.".format(uri, exc))
        exit_from_event_loop_thread(loop, stop)
        return
    else:
        print_during_input("Connected to {}.".format(uri))

    try:
        while True:
            incoming = asyncio.ensure_future(websocket.recv())
            outgoing = asyncio.ensure_future(inputs.get())
            done, pending = await asyncio.wait(
                [incoming, outgoing, stop], return_when=asyncio.FIRST_COMPLETED
            )

            # Cancel pending tasks to avoid leaking them.
            if incoming in pending:
                incoming.cancel()
            if outgoing in pending:
                outgoing.cancel()

            if incoming in done:
                try:
                    message = incoming.result()
                except websockets.ConnectionClosed:
                    break
                else:
                    print_during_input("< " + message)

            if outgoing in done:
                message = outgoing.result()
                await websocket.send(message)

            if stop in done:
                break

    finally:
        await websocket.close()
        close_status = format_close(websocket.close_code, websocket.close_reason)

        print_over_input(
            "Connection closed: {close_status}.".format(close_status=close_status)
        )

        exit_from_event_loop_thread(loop, stop)


def main():
    # If we're on Windows, enable VT100 terminal support.
    if os.name == "nt":
        try:
            win_enable_vt100()
        except RuntimeError as exc:
            sys.stderr.write(
                (
                    "Unable to set terminal to VT100 mode. This is only "
                    "supported since Win10 anniversary update. Expect "
                    "weird symbols on the terminal.\nError: {exc!s}\n"
                ).format(exc=exc)
            )
            sys.stderr.flush()

    # Parse command line arguments.
    parser = argparse.ArgumentParser(
        prog="python -m websockets",
        description="Interactive WebSocket client.",
        add_help=False,
    )
    parser.add_argument("uri", metavar="<uri>")
    args = parser.parse_args()

    # Create an event loop that will run in a background thread.
    loop = asyncio.new_event_loop()

    # Create a queue of user inputs. There's no need to limit its size.
    inputs = asyncio.Queue(loop=loop)

    # Create a stop condition when receiving SIGINT or SIGTERM.
    stop = asyncio.Future(loop=loop)

    # Schedule the task that will manage the connection.
    asyncio.ensure_future(run_client(args.uri, loop, inputs, stop), loop=loop)

    # Start the event loop in a background thread.
    thread = threading.Thread(target=loop.run_forever)
    thread.start()

    # Read from stdin in the main thread in order to receive signals.
    try:
        while True:
            # Since there's no size limit, put_nowait is identical to put.
            message = input("> ")
            loop.call_soon_threadsafe(inputs.put_nowait, message)
    except (KeyboardInterrupt, EOFError):  # ^C, ^D
        loop.call_soon_threadsafe(stop.set_result, None)

    # Wait for the event loop to terminate.
    thread.join()


if __name__ == "__main__":
    main()
