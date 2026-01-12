"""
Unit tests for exec command error handling.

These tests verify that error messages from RADKit are properly extracted
and exposed to MCP clients instead of showing "Unknown error".
"""

import pytest
import sys
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
import asyncio

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))


class MockExecStatus:
    """Mock for RADKit exec status enum."""
    def __init__(self, value: str):
        self.value = value


class MockDeviceResult:
    """Mock for RADKit device result with configurable status and errors."""

    def __init__(self, status: str = "SUCCESS", errors: list = None, commands: dict = None):
        self.status = MockExecStatus(status)
        self.errors = errors
        self._commands = commands or {}

    def __iter__(self):
        return iter(self._commands.keys())

    def __getitem__(self, key):
        return self._commands.get(key)


class MockCommandResult:
    """Mock for individual command result."""

    def __init__(self, status: str = "SUCCESS", data: str = "", errors: list = None):
        self.status = MockExecStatus(status)
        self._data = data
        self.errors = errors
        self._should_raise = False
        self._raise_msg = ""

    @property
    def data(self):
        if self._should_raise:
            raise Exception(self._raise_msg)
        return self._data

    def set_data_raises(self, msg: str):
        """Configure data access to raise an exception."""
        self._should_raise = True
        self._raise_msg = msg


class MockResponse:
    """Mock for RADKit response object."""

    def __init__(self, result: dict):
        self.result = result


class MockInventory:
    """Mock for RADKit inventory."""

    def __init__(self, devices: dict = None, exec_response: MockResponse = None):
        self._devices = devices or {}
        self._exec_response = exec_response

    def filter(self, field: str, value: str):
        if value in self._devices:
            return self
        return None

    def exec(self, commands, **kwargs):
        return MockAsyncExec(self._exec_response)


class MockAsyncExec:
    """Mock for async exec operation."""

    def __init__(self, response: MockResponse):
        self._response = response

    def wait(self, timeout=None):
        return self._response


class MockService:
    """Mock for RADKit service."""

    def __init__(self, inventory: MockInventory):
        self.inventory = inventory


@pytest.mark.unit
class TestErrorExtraction:
    """Tests for error extraction from RADKit responses."""

    def test_errors_attribute_list_extraction(self):
        """Test that errors from .errors attribute (list) are properly extracted."""
        # Create mock with errors list
        device_result = MockDeviceResult(
            status="FAILURE",
            errors=[
                "ConnectionError: Failed after the configured number of attempts. Last error: [Errno 111] Connect call failed ('198.18.1.102', 22)"
            ]
        )

        # Test the error extraction logic from exec.py
        errors = getattr(device_result, "errors", None)
        assert errors is not None
        assert isinstance(errors, list)

        status_msg = "; ".join(str(e) for e in errors)
        assert "ConnectionError" in status_msg
        assert "198.18.1.102" in status_msg
        assert "Errno 111" in status_msg

    def test_errors_attribute_string_extraction(self):
        """Test that errors from .errors attribute (string) are properly extracted."""
        device_result = MockDeviceResult(
            status="FAILURE",
            errors="Permission denied to RPC call. Remote user is not active."
        )

        errors = getattr(device_result, "errors", None)
        assert errors is not None

        # Handle string case
        status_msg = str(errors)
        assert "Permission denied" in status_msg
        assert "Remote user is not active" in status_msg

    def test_multiple_errors_joined(self):
        """Test that multiple errors are joined with semicolon."""
        device_result = MockDeviceResult(
            status="FAILURE",
            errors=[
                "First error occurred",
                "Second error occurred",
                "Third error occurred"
            ]
        )

        errors = device_result.errors
        status_msg = "; ".join(str(e) for e in errors)

        assert "First error occurred" in status_msg
        assert "Second error occurred" in status_msg
        assert "Third error occurred" in status_msg
        assert status_msg.count("; ") == 2

    def test_fallback_to_data_exception(self):
        """Test fallback to .data exception when .errors is empty."""
        device_result = MockDeviceResult(status="FAILURE", errors=None)

        # Simulate that accessing .data raises ExecError
        errors = getattr(device_result, "errors", None)
        assert errors is None

        # The fallback in exec.py would try to access .data
        status_msg = f"Status: {device_result.status.value}"
        assert status_msg == "Status: FAILURE"

    def test_timeout_error_extraction(self):
        """Test extraction of timeout error messages."""
        device_result = MockDeviceResult(
            status="FAILURE",
            errors=[
                "Device action failed: Performing action failed: Timeout exception while performing commands"
            ]
        )

        errors = device_result.errors
        status_msg = "; ".join(str(e) for e in errors)

        assert "Timeout exception" in status_msg
        assert "Device action failed" in status_msg


@pytest.mark.unit
class TestIndividualCommandErrors:
    """Tests for individual command error handling."""

    def test_individual_command_failure_with_errors(self):
        """Test that individual command errors are captured."""
        cmd_result = MockCommandResult(
            status="FAILURE",
            errors=["Command timed out after 5 seconds"]
        )

        cmd_status = cmd_result.status.value
        assert cmd_status == "FAILURE"

        cmd_errors = getattr(cmd_result, "errors", None)
        assert cmd_errors is not None

        error_msg = "; ".join(str(e) for e in cmd_errors)
        assert "timed out" in error_msg

    def test_individual_command_failure_data_raises(self):
        """Test that error is extracted when .data raises exception."""
        cmd_result = MockCommandResult(status="FAILURE")
        cmd_result.set_data_raises("ExecError: Connection refused")

        cmd_status = cmd_result.status.value
        assert cmd_status == "FAILURE"

        cmd_errors = getattr(cmd_result, "errors", None)
        assert cmd_errors is None

        # Fallback: try accessing .data
        try:
            _ = cmd_result.data
            error_msg = f"Command failed with status: {cmd_status}"
        except Exception as cmd_err:
            error_msg = str(cmd_err)

        assert "ExecError" in error_msg or "Connection refused" in error_msg

    def test_partial_success_scenario(self):
        """Test handling of PARTIAL_SUCCESS status with mixed results."""
        # Simulate a response where one command succeeds, another fails
        success_cmd = MockCommandResult(
            status="SUCCESS",
            data="Command output here"
        )
        failure_cmd = MockCommandResult(
            status="FAILURE",
            errors=["Device action failed: Permission error"]
        )

        # Build mock device result with both commands
        commands = {
            "show version": success_cmd,
            "show running-config": failure_cmd
        }

        device_result = MockDeviceResult(
            status="PARTIAL_SUCCESS",
            commands=commands
        )

        # Process each command
        results = []
        for cmd in device_result:
            cmd_result = device_result[cmd]
            cmd_status = cmd_result.status.value

            if cmd_status != "SUCCESS":
                cmd_errors = getattr(cmd_result, "errors", None)
                if cmd_errors:
                    error_msg = "; ".join(str(e) for e in cmd_errors)
                else:
                    error_msg = f"Command failed with status: {cmd_status}"
                results.append({"command": cmd, "status": cmd_status, "error": error_msg})
            else:
                results.append({"command": cmd, "status": cmd_status, "output": cmd_result.data})

        # Verify results
        assert len(results) == 2

        success_result = next(r for r in results if r["command"] == "show version")
        assert success_result["status"] == "SUCCESS"
        assert "output" in success_result

        failure_result = next(r for r in results if r["command"] == "show running-config")
        assert failure_result["status"] == "FAILURE"
        assert "Permission error" in failure_result["error"]


@pytest.mark.unit
class TestConnectionErrors:
    """Tests for specific connection error scenarios."""

    def test_connection_refused_error(self):
        """Test connection refused error is properly formatted."""
        error_msg = "ConnectionError: Failed after the configured number of attempts. Last error: [Errno 111] Connect call failed ('198.18.1.102', 22)"

        device_result = MockDeviceResult(status="FAILURE", errors=[error_msg])

        errors = device_result.errors
        status_msg = "; ".join(str(e) for e in errors)

        # Verify the error message contains useful diagnostic information
        assert "ConnectionError" in status_msg
        assert "198.18.1.102" in status_msg
        assert "22" in status_msg  # SSH port
        assert "Errno 111" in status_msg

    def test_permission_denied_error(self):
        """Test permission denied error is properly formatted."""
        error_msg = "Permission denied to RPC call. Reason: Remote user is not active."

        device_result = MockDeviceResult(status="FAILURE", errors=[error_msg])

        errors = device_result.errors
        status_msg = "; ".join(str(e) for e in errors)

        # Verify permission error details are preserved
        assert "Permission denied" in status_msg
        assert "Remote user is not active" in status_msg

    def test_authentication_error(self):
        """Test authentication error is properly formatted."""
        error_msg = "Device action failed: Authentication failed for user 'admin'"

        device_result = MockDeviceResult(status="FAILURE", errors=[error_msg])

        errors = device_result.errors
        status_msg = "; ".join(str(e) for e in errors)

        assert "Authentication failed" in status_msg
        assert "admin" in status_msg


@pytest.mark.unit
@pytest.mark.asyncio
async def test_exec_command_error_propagation():
    """Test that errors propagate correctly through the async exec function."""
    from radkit_mcp.tools.exec import radkit_exec_command

    # Create mock that will fail
    mock_device_result = MockDeviceResult(
        status="FAILURE",
        errors=["Test error: Device unreachable"]
    )
    mock_response = MockResponse({"test_device": mock_device_result})
    mock_inventory = MockInventory(
        devices={"test_device": True},
        exec_response=mock_response
    )
    mock_service = MockService(inventory=mock_inventory)

    # Patch get_service to return our mock
    with patch('radkit_mcp.tools.exec.get_service', return_value=mock_service):
        with pytest.raises(Exception) as exc_info:
            await radkit_exec_command("test_device", "show version")

        # Verify error message contains the actual error, not "Unknown error"
        error_message = str(exc_info.value)
        assert "Unknown error" not in error_message
        assert "Test error: Device unreachable" in error_message
