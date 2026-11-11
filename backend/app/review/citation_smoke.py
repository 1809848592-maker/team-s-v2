"""显式开启的公开书目接口冒烟；不加载Settings、数据库、模型或.env。"""

import argparse
import json
import os

from pydantic import SecretStr

from app.review.citation_databases import CitationDatabaseClient, CitationQuery


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="仅核对一条公开书目，不调用模型或发送交付正文。")
    parser.add_argument(
        "--live-public-apis", action="store_true", help="明确允许查询公开数据库接口"
    )
    args = parser.parse_args(argv)
    if not args.live_public_apis:
        parser.error("须显式提供--live-public-apis，默认不访问外部接口。")
    # 可选Key只用于OpenAlex Bearer；不读.env，也不读取DeepSeek环境变量。
    value = os.environ.get("OPENALEX_API_KEY", "")
    client = CitationDatabaseClient(openalex_api_key=SecretStr(value) if value else None)
    result = client.verify(
        CitationQuery(
            title="The measurement of observer agreement for categorical data",
            authors=("Landis", "Koch"),
            year=1977,
            doi="10.2307/2529310",
            is_chinese=False,
        )
    )
    print(
        json.dumps(
            {
                "public_reference": "Landis & Koch (1977)",
                "verification": result.to_dict(),
                "model_calls": 0,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if result.status == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
