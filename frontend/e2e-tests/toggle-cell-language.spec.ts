/* Copyright 2026 Marimo. All rights reserved. */
import { copyFile } from "node:fs/promises";
import { expect, test } from "@playwright/test";
import { getAppUrl } from "../playwright.config";
import { openCellActions, openCommandPalette } from "./helper";

test.beforeEach(async ({ page }, testInfo) => {
  // A unique file also gives each test and retry its own kernel session.
  const filename = testInfo.outputPath("notebook.py");
  await copyFile("e2e-tests/py/title.py", filename);
  const appUrl = new URL(getAppUrl("title.py"));
  appUrl.searchParams.set("file", filename);
  await page.goto(appUrl.toString());

  const title = page.getByRole("heading", { name: "Hello Marimo!", exact: true });
  await expect(title).toBeVisible();
  await openCellActions(page, title);
  await page.getByText("Convert to Markdown", { exact: true }).click();
  await expect(title).toBeVisible();

  // Converted Markdown stays visible until the cell loses focus.
  await page.keyboard.press("Escape");
  await page.locator("body").click({ position: { x: 4, y: 4 } });
  await expect(page.getByTestId("cell-editor")).toBeHidden();
});

test("dropdown toggles Markdown code and focuses the cell", async ({ page }) => {
  const title = page.getByRole("heading", { name: "Hello Marimo!", exact: true });
  const editor = page.getByTestId("cell-editor");

  await openCellActions(page, title);
  await page.getByText("Show code", { exact: true }).click();
  await expect(editor).toBeVisible();

  await openCellActions(page, title);
  await page.getByText("Hide code", { exact: true }).click();
  await expect(editor).toBeHidden();
  await expect(page.locator(".marimo-cell")).toBeFocused();
});

test("right-click Hide code focuses the cell after closing", async ({ page }) => {
  const title = page.getByRole("heading", { name: "Hello Marimo!", exact: true });
  const editor = page.getByTestId("cell-editor");
  await openCellActions(page, title);
  await page.getByText("Show code", { exact: true }).click();
  await expect(editor).toBeVisible();

  const editorInput = editor.locator(".cm-content");
  await editorInput.click();
  await editorInput.click({ button: "right" });
  await page.getByRole("menuitem", { name: /^Hide code/ }).click();
  await expect(editor).toBeHidden();
  await expect(page.locator(".marimo-cell")).toBeFocused();
});

test("dependency-graph Hide code keeps focus in the graph", async ({ page }) => {
  await openCommandPalette({ page, command: "Helper panel > Dependencies" });
  await page.getByRole("tab", { name: "Graph", exact: true }).click();
  await page.locator(".react-flow__panel .lucide-settings").click();
  await page.getByTestId("hide-pure-markdown-checkbox").uncheck();
  await page.keyboard.press("Escape");
  await page.locator(".react-flow__node").first().click();

  const graphActions = page.getByRole("button", {
    name: "Cell actions",
    exact: true,
  });
  await graphActions.click();
  await page.getByText("Show code", { exact: true }).click();
  await expect(page.getByTestId("cell-editor")).toBeVisible();

  await graphActions.click();
  await page.getByText("Hide code", { exact: true }).click();
  await expect(page.getByTestId("cell-editor")).toBeHidden();
  await expect(graphActions).toBeFocused();
});
