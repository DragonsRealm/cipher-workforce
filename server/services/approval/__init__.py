"""Approval services for DCS soul dispatch gate."""
from .governor import ApprovalGovernor, HumanApprovalQueue

__all__ = ["ApprovalGovernor", "HumanApprovalQueue"]
