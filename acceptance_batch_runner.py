"""Host adapter for concurrently executing read-only acceptance shards."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Mapping, Optional

from kernel.acceptance import AcceptanceBatch, AcceptanceShard


def run_acceptance_batch(
    batch: AcceptanceBatch,
    reviewer: Callable[
        [AcceptanceShard, Mapping[str, object]],
        Mapping[str, object],
    ],
    *,
    verify_attestation: Callable[
        [Mapping[str, object]], bool
    ],
    max_workers: Optional[int] = None,
) -> dict:
    """Run independent shards in parallel; aggregation stays in the core."""
    frozen_json = json.dumps(
        {
            "batch": batch.to_dict(),
            "package": batch.package.to_dict(),
        },
        ensure_ascii=False,
        sort_keys=True,
    )

    def execute(shard: AcceptanceShard) -> Mapping[str, object]:
        if not shard.read_only:
            raise ValueError(
                f"acceptance shard '{shard.shard_id}' is not read-only"
            )
        return reviewer(shard, json.loads(frozen_json))

    with ThreadPoolExecutor(
        max_workers=max_workers or len(batch.shards)
    ) as executor:
        results = list(executor.map(execute, batch.shards))
    return batch.aggregate(
        results,
        verify_attestation=verify_attestation,
    )
