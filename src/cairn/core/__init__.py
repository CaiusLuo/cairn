"""Adapter-independent contracts, with one owner per domain.

``state`` owns conversation facts over ``models`` message/LLM/tool values;
``context`` builds bounded request views without mutating those facts. Run step
limits belong to ``budget``. ``model_manager`` owns session selection and
``provider_catalog`` holds declared providers. ``tasks`` and ``repository`` hold
Task/evidence values, ``events`` and ``llm`` define communication contracts,
``permissions`` owns policy, and ``memory`` owns evidence-backed memory values.

These are independent modules, not a general state or storage abstraction.
Import from the owning module; this package does not re-export domain APIs.
"""
