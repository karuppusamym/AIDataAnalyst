"""Harnesses shared by the Tier-0 invariant suite (tracker ST-03).

Nothing in this package asserts anything. It exists so that each invariant test
can be *data-driven* -- enumerating every route, every connector, every mapped
column -- instead of naming a handful of examples that stop covering the system
the day someone adds the next one.
"""
