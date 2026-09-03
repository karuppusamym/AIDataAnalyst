import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { OrgProvider } from "./lib/org";
import "./tokens.css";

const el = document.getElementById("root");
if (!el) throw new Error("#root is missing from index.html");

createRoot(el).render(
  <StrictMode>
    <OrgProvider>
      <App />
    </OrgProvider>
  </StrictMode>,
);
