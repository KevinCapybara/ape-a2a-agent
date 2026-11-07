import asyncio
import re
from functools import cache
from random import shuffle

from playwright.async_api import async_playwright


GAME_URL = "https://ttt.puppy9.com/"
CELLS = "#gameBoard .cell"
WIN_LINES = (
    (0, 1, 2), (3, 4, 5), (6, 7, 8),
    (0, 3, 6), (1, 4, 7), (2, 5, 8),
    (0, 4, 8), (2, 4, 6),
)


def play_move(board: tuple[str, ...], index: int, mark: str) -> tuple[str, ...]:
    return board[:index] + (mark,) + board[index + 1:]


@cache
def score(board: tuple[str, ...], turn: str) -> int:
    for a, b, c in WIN_LINES:
        if board[a] and board[a] == board[b] == board[c]:
            points = 1 + board.count("")
            return points if board[a] == "X" else -points
    if "" not in board:
        return 0

    next_turn = "O" if turn == "X" else "X"
    scores = (
        score(play_move(board, index, turn), next_turn)
        for index, cell in enumerate(board)
        if not cell
    )
    return max(scores) if turn == "X" else min(scores)


async def win_tic_tac_toe() -> str:
    async with asyncio.timeout(75), async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="msedge", headless=True)
        try:
            page = await browser.new_page()
            page.set_default_timeout(5_000)
            await page.goto(GAME_URL, wait_until="domcontentloaded", timeout=20_000)
            cells = page.locator(CELLS)
            status = page.locator("#gameStatus")
            congratulations = page.locator("#congratulations.show")

            for attempt in range(12):
                if attempt:
                    await page.locator("#resetButton").click()

                while "status-playing" in (await status.get_attribute("class") or "").split():
                    board = tuple(cell.strip() for cell in await cells.all_text_contents())
                    moves = [index for index, cell in enumerate(board) if not cell]
                    shuffle(moves)
                    move = max(moves, key=lambda index: score(play_move(board, index, "X"), "O"))
                    await cells.nth(move).click()
                    await page.wait_for_function(
                        """expected => {
                            const playing = document.querySelector('#gameStatus')
                                .classList.contains('status-playing');
                            const opponentMoves = [...document.querySelectorAll('#gameBoard .cell')]
                                .filter(cell => cell.textContent.trim() === 'O').length;
                            return !playing || opponentMoves === expected;
                        }""",
                        arg=board.count("O") + 1,
                        timeout=5_000,
                    )

                if await congratulations.is_visible():
                    match = re.search(r"(?<![0-9])[0-9]{14}(?![0-9])", await congratulations.inner_text())
                    if match:
                        return match.group()
                    raise RuntimeError("The win message did not contain a 14-digit number.")
        finally:
            await browser.close()

    raise RuntimeError("The browser did not win within 12 games.")
