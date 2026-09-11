import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { ExcelAddinPane } from "./ExcelAddinPane";
import { connectWorkbookHost } from "./officeHost";
import "../tokens.css";
import "../layout.css";
import "./excelAddin.css";

const element = document.getElementById("root");
if (!element) throw new Error("#root is missing from excel-addin.html");

/* The host is resolved before the first render: whether this page is inside
 * Excel decides everything the pane shows, and rendering first would flash the
 * "open this inside Excel" message at someone who is. */
void connectWorkbookHost().then((host) => {
  createRoot(element).render(
    <StrictMode>
      <ExcelAddinPane host={host} />
    </StrictMode>,
  );
});
