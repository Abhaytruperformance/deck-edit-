"""Pydantic models for the Artifact Contract block types.

Array position is the only ordering signal — there is no `order` field.
Every renderer consumes this same discriminated union.
"""
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field


class TitleSlideContent(BaseModel):
    headline: str
    subhead: str


class TextBlockContent(BaseModel):
    heading: str
    body: str


class KpiItem(BaseModel):
    label: str
    value: float
    unit: str
    change_pct: float | None = None


class KpiGridContent(BaseModel):
    title: str
    items: list[KpiItem]


class BulletListContent(BaseModel):
    heading: str
    items: list[str]


class ImageBlockContent(BaseModel):
    caption: str
    image_ref: str  # Supabase Storage path


class ComparisonTableContent(BaseModel):
    headers: list[str]
    rows: list[list[str]]


class ChartSeries(BaseModel):
    name: str
    values: list[float]


class ChartContent(BaseModel):
    chart_type: Literal["bar", "line", "pie"]
    title: str
    labels: list[str]
    series: list[ChartSeries]


class TitleSlideBlock(BaseModel):
    id: str
    type: Literal["title_slide"]
    enabled: bool
    content: TitleSlideContent


class TextBlockBlock(BaseModel):
    id: str
    type: Literal["text_block"]
    enabled: bool
    content: TextBlockContent


class KpiGridBlock(BaseModel):
    id: str
    type: Literal["kpi_grid"]
    enabled: bool
    content: KpiGridContent


class BulletListBlock(BaseModel):
    id: str
    type: Literal["bullet_list"]
    enabled: bool
    content: BulletListContent


class ImageBlockBlock(BaseModel):
    id: str
    type: Literal["image_block"]
    enabled: bool
    content: ImageBlockContent


class ComparisonTableBlock(BaseModel):
    id: str
    type: Literal["comparison_table"]
    enabled: bool
    content: ComparisonTableContent


class ChartBlock(BaseModel):
    id: str
    type: Literal["chart"]
    enabled: bool
    content: ChartContent


Block = Annotated[
    Union[
        TitleSlideBlock,
        TextBlockBlock,
        KpiGridBlock,
        BulletListBlock,
        ImageBlockBlock,
        ComparisonTableBlock,
        ChartBlock,
    ],
    Field(discriminator="type"),
]


class ArtifactBlocks(BaseModel):
    """Wrapper used to validate a full blocks array (e.g. AI draft output)."""

    blocks: list[Block]
