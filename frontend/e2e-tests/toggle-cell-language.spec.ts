/* Copyright 2026 Marimo. All rights reserved. */
import { expect, test } from "@playwright/test";
import { getAppUrl, resetFile } from "../playwright.config";
import {
  maybeRestartKernel,
  openCellActions,
  openCommandPalette,
} from "./helper";

const appUrl = getAppUrl("title.py");

test.beforeEach(async ({ page }, info) => {
  await page.goto(appUrl);
  if (info.retry) {
    await page.reload();
  }
  await maybeRestartKernel(page);
});

test.afterEach(async () => {
  // Need to reset the file because this test modifies it
  await resetFile("title.py");
});

test("change the cell to a markdown cell and toggle hide code", async ({
  page,
}) => {
  const title = page.getByText("Hello Marimo!", { exact: true });
  await expect(title).toBeVisible();
  const cellEditor = page.getByTestId("cell-editor");
  const cell = page.locator(".marimo-cell").first();

  // Convert to Markdown
  await openCellActions(page, title);
  await page.getByText("Convert to Markdown").click();
  await expect(title).toBeVisible();

  // Code stays visible while the cell is focused; blur it so the markdown
  // initial hide-code takes effect before asserting.
  await page.keyboard.press("Escape");
  await page.locator("body").click({ position: { x: 4, y: 4 } });

  // Expect cell editor to be invisible at first (markdown initial hide code is true)
  await expect(cellEditor).toBeHidden();

  // Unhide code
  await openCellActions(page, title);
  await page.getByText("Show code").click();
  await expect(cellEditor).toBeVisible();

  // Hide code
  await openCellActions(page, title);
  await page.getByText("Hide code").click();
  await expect(cellEditor).toBeHidden();
  await expect(cell).toBeFocused();

  await openCellActions(page, title);
  await page.getByText("Show code", { exact: true }).click();
  const editorInput = cellEditor.locator(".cm-content");
  await editorInput.click();
  await editorInput.click({ button: "right" });
  await page.getByRole("menuitem", { name: /^Hide code/ }).click();
  await expect(cellEditor).toBeHidden();
  await expect(cell).toBeFocused();

  // Graph actions keep keyboard focus in the graph panel.
  await openCommandPalette({ page, command: "Helper panel > Dependencies" });
  await page.getByRole("tab", { name: "Graph", exact: true }).click();
  await page.locator(".react-flow__panel .lucide-settings").click();
  await page.getByTestId("hide-pure-markdown-checkbox").uncheck();
  await page.keyboard.press("Escape");
  await page.locator(".react-flow__node").first().click();
  const graphActions = page.getByRole("button", { name: "Cell actions", exact: true });
  await graphActions.click();
  await page.getByText("Show code", { exact: true }).click();
  await expect(cellEditor).toBeVisible();
  await graphActions.click();
  await page.getByText("Hide code", { exact: true }).click();
  await expect(cellEditor).toBeHidden();
  await expect(graphActions).toBeFocused();
});
