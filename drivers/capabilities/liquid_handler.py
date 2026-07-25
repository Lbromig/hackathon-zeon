"""Capability interface for liquid handlers (e.g. the Opentrons)."""
from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass

from ..base import InstrumentDriver, InstrumentKind


@dataclass
class DeckLocation:
    """A point the pipette can reach: a labware slot/well, or a taught XYZ."""
    slot: str | None = None
    well: str | None = None
    x: float | None = None
    y: float | None = None
    z: float | None = None


class LiquidHandlerDriver(InstrumentDriver):
    kind = InstrumentKind.LIQUID_HANDLER

    @abstractmethod
    def home(self) -> None: ...

    @abstractmethod
    def pick_up_tip(self, location: DeckLocation) -> None: ...

    @abstractmethod
    def drop_tip(self, location: DeckLocation | None = None) -> None: ...

    @abstractmethod
    def aspirate(self, volume_ul: float, location: DeckLocation) -> None: ...

    @abstractmethod
    def dispense(self, volume_ul: float, location: DeckLocation) -> None: ...

    @abstractmethod
    def move_to(self, location: DeckLocation) -> None: ...
