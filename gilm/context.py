from __future__ import annotations

from .models import ChatRequest, content_hash


def transform(request: ChatRequest, remove_redundant: bool):
    """Validate all annotations before making any change; never parse instructions in data."""
    blocks = request.gilm.blocks
    ids = {block.id: block for block in blocks}
    if len(ids) != len(blocks) or len({b.message_index for b in blocks}) != len(blocks):
        return request, [], "duplicate_context_identity"
    current_user = max((i for i, m in enumerate(request.messages) if m.role == "user"), default=-1)
    for block in blocks:
        if block.message_index >= len(request.messages):
            return request, [], "missing_context_message"
        message = request.messages[block.message_index]
        if message.role != block.role or block.content_hash != content_hash(message.content or ""):
            return request, [], "context_integrity_failed"
        if not set(block.dependencies) <= ids.keys():
            return request, [], "missing_dependency"
    if not remove_redundant:
        return request, [], None
    removed = set()
    for block in blocks:
        if not block.eligible or not block.redundant_with:
            continue
        message = request.messages[block.message_index]
        if (
            block.required
            or block.protected
            or block.retention != "removable"
            or message.role != "user"
            or block.message_index == current_user
        ):
            return request, [], "protected_context"
        target = ids.get(block.redundant_with)
        if (
            target is None
            or target.id == block.id
            or target.role != block.role
            or target.content_hash != block.content_hash
            or target.source != block.source
            or target.source_version != block.source_version
            or request.messages[target.message_index].model_dump() != message.model_dump()
        ):
            return request, [], "invalid_redundancy"
        removed.add(block.id)
    if any(b.redundant_with in removed for b in blocks if b.id in removed):
        return request, [], "redundancy_target_removed"
    if any(set(b.dependencies) & removed for b in blocks if b.id not in removed):
        return request, [], "dependency_would_be_removed"
    indices = {ids[ident].message_index for ident in removed}
    result = request.model_copy(deep=True)
    result.messages = [message for i, message in enumerate(request.messages) if i not in indices]
    return result, sorted(removed), None
