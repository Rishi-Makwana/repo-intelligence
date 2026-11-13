from fastapi import APIRouter

router = APIRouter()


class Quote:
    def total(self, items: list[int]) -> int:
        return sum(items)


@router.get("/quotes/{quote_id}")
async def read_quote(quote_id: str):
    return {}
