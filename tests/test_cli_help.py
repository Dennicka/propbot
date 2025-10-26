"""Smoke tests for the operator CLI parser."""

import argparse
import importlib


def test_cli_parser_exposes_status_command() -> None:
    module = importlib.import_module("cli.propbotctl")
    parser = module.build_parser()
    assert isinstance(parser, argparse.ArgumentParser)

    subparsers_actions = [
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)  # type: ignore[attr-defined]
    ]
    assert subparsers_actions, "Expected a subparsers action to be registered"
    status_present = any("status" in action.choices for action in subparsers_actions)
    assert status_present, "status command should be registered on the CLI"
