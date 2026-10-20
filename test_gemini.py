"""Manual, opt-in live check for the Gemini AI layer (makes a REAL paid API call).

Only runs when executed directly:  python test_gemini.py
pytest may import this file during collection, but the guard below
prevents any API call unless the script is run on purpose.
"""

from app.services.ai_service import generate_support_response


def main() -> None:
    answer = generate_support_response(
        "how can i forgot my password? give me the answer in 4 points"
    )
    print(answer)


if __name__ == "__main__":
    main()

