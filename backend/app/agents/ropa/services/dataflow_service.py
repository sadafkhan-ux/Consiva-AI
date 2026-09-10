"""Data-flow mapping (ROPA prompt §10).

The prompt's constraint is the whole design here: "Do not invent intermediate
systems. Unknown links must remain unknown." So a flow path is built ONLY from
nodes that exist in the evidence -- the source system, its storage location, and
any vendor/processor actually present in evidence.vendors. Nothing is inserted
to make the diagram look complete, and a flow that reaches no processor is
flagged for review rather than being padded with a plausible third party.
"""

from __future__ import annotations

from app.agents.ropa.schemas.evidence import DiscoveryEvidence
from app.agents.ropa.schemas.output import DataFlowMapping
from app.agents.ropa.schemas.ropa import ProcessingActivity


def build_data_flows(
    evidence: DiscoveryEvidence,
    activities: list[ProcessingActivity],
) -> list[DataFlowMapping]:
    flows: list[DataFlowMapping] = []
    vendors = evidence.vendors

    for activity in activities:
        for system in activity.source_systems:
            source = next((s for s in evidence.sources if s.name == system), None)
            path = [system]
            flow_evidence = list(activity.evidence)

            if source is not None:
                flow_evidence.append(source.local_id)
                # The storage node is the connector + location actually observed,
                # not an assumed database tier.
                storage = source.connector or source.source_type
                if source.location:
                    storage = f"{storage} ({source.location})"
                path.append(storage)

            downstream = [v for v in vendors if source is None or v.integration_local_id == source.local_id]
            for vendor in downstream:
                path.append(vendor.name)
                flow_evidence.append(vendor.local_id)

            flows.append(
                DataFlowMapping(
                    processing_activity=activity.name,
                    path=path,
                    evidence=sorted(set(flow_evidence)),
                    # No evidenced processor/recipient means the flow is
                    # incomplete, not that the data stays put.
                    review_required=not downstream,
                )
            )

    return flows
