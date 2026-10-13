"""Known-VIOLATING adapter — free-form query string reaching pandas.query."""
from pydantic import BaseModel, ConfigDict

from adapters.base import ToolSpec


class In(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query_string: str


def run(ws, query_string: str) -> dict:
    import pandas as pd

    df = pd.DataFrame()
    return {"result": str(df.query(query_string))}  # free-form expression


TOOLS = [ToolSpec("adv_query", "query dataframes freely", In, run)]
