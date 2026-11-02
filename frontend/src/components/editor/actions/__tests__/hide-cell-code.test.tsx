/* Copyright 2026 Marimo. All rights reserved. */
// @vitest-environment jsdom

import { act, renderHook } from "@testing-library/react";
import { createStore } from "jotai";
import { expect, it, onTestFinished, vi } from "vitest";
import { DocumentWrapper, mockDocumentModel } from "@/__mocks__/document";
import { MockNotebook } from "@/__mocks__/notebook";
import { MockRequestClient } from "@/__mocks__/requests";
import { cellId } from "@/__tests__/branded";
import { notebookAtom } from "@/core/cells/cells";
import { requestClientAtom } from "@/core/network/requests";
import { temporarilyShownCodeAtom } from "../../navigation/state";
import { useCellActionButtons } from "../useCellActionButton";

it("hides an untouched, temporarily shown cell before its editor mounts", () => {
  const store = createStore();
  store.set(requestClientAtom, MockRequestClient.create());
  const cid = cellId("c1");
  const { model, stop } = mockDocumentModel(store);
  onTestFinished(stop);
  const notebook = MockNotebook.notebookState({ cellData: { [cid]: {} } });
  notebook.untouchedNewCells.add(cid);
  store.set(notebookAtom, notebook);
  store.set(temporarilyShownCodeAtom, new Set([cid]));
  const onHideCode = vi.fn();

  const { result } = renderHook(
    () =>
      useCellActionButtons({
        cell: {
          ...notebook.cellData[cid],
          cellId: cid,
          status: "idle",
          hasOutput: false,
          hasConsoleOutput: false,
          getEditorView: () => null,
        },
        onHideCode,
      }),
    {
      wrapper: ({ children }) => (
        <DocumentWrapper store={store} model={model}>
          {children}
        </DocumentWrapper>
      ),
    },
  );
  const hideCode = result.current
    .flat()
    .find((action) => action.label === "Hide code");
  expect(hideCode).toBeDefined();
  act(() => hideCode?.handle());

  expect({
    hidden: store.get(model.documentAtom)?.cells.get(cid)?.config.hide_code,
    temporarilyShown: [...store.get(temporarilyShownCodeAtom)],
    untouched: [...store.get(notebookAtom).untouchedNewCells],
  }).toEqual({ hidden: true, temporarilyShown: [], untouched: [] });
  expect(onHideCode).toHaveBeenCalledExactlyOnceWith(cid);
});
