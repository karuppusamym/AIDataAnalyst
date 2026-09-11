/* ---------------------------------------------------------------------------
   The Office.js calls the Excel add-in makes, behind one small interface.

   Office.js is Microsoft's runtime, loaded from their CDN by the add-in's HTML
   page. It is not an npm dependency and has no types in this project, and
   widening the whole type surface for it would be the wrong trade. This module
   names exactly the calls the add-in makes and reads them off `globalThis` --
   which is also what lets tests hand the pane a fake host.

   Outside Excel (the page opened in a normal browser) every operation rejects
   with a message saying where to open it, rather than failing on an undefined
   global somewhere deeper.
--------------------------------------------------------------------------- */

interface AsyncResult<T> {
  status: string;
  value: T;
  error?: { message?: string };
}

interface OfficeSlice {
  data: ArrayLike<number> | ArrayBuffer;
}

interface OfficeFile {
  sliceCount: number;
  getSliceAsync(index: number, callback: (result: AsyncResult<OfficeSlice>) => void): void;
  closeAsync(callback?: () => void): void;
}

interface OfficeDialog {
  addEventHandler(type: string, handler: (arg: { message?: string; error?: number }) => void): void;
  close(): void;
}

export interface OfficeLike {
  onReady(): Promise<{ host: string | null; platform?: string | null }>;
  AsyncResultStatus: { Succeeded: string };
  FileType: { Compressed: string };
  EventType: { DialogMessageReceived: string; DialogEventReceived: string };
  context: {
    document: {
      url?: string | null;
      getFileAsync(
        fileType: string,
        options: { sliceSize: number },
        callback: (result: AsyncResult<OfficeFile>) => void,
      ): void;
    };
    ui: {
      displayDialogAsync(
        url: string,
        options: { height: number; width: number; promptBeforeOpen?: boolean },
        callback: (result: AsyncResult<OfficeDialog>) => void,
      ): void;
    };
  };
}

interface ExcelRange {
  values: unknown[][];
  load(properties: string): void;
}

interface ExcelWorksheet {
  isNullObject: boolean;
  getUsedRange(valuesOnly?: boolean): ExcelRange;
}

interface ExcelContext {
  workbook: { worksheets: { getItemOrNullObject(name: string): ExcelWorksheet } };
  sync(): Promise<void>;
}

export interface ExcelLike {
  run<T>(batch: (context: ExcelContext) => Promise<T>): Promise<T>;
  createWorkbook(base64: string): Promise<void>;
}

export interface WorkbookHost {
  /** False when this page is not running inside Excel. */
  readonly inExcel: boolean;
  /** A sheet's used range as strings, or null when the sheet does not exist. */
  readSheet(name: string): Promise<string[][] | null>;
  /** The whole open workbook as .xlsx bytes, as Excel holds it now. */
  readWorkbookFile(): Promise<Uint8Array>;
  /** Open .xlsx bytes as a new workbook window. */
  openWorkbook(bytes: Uint8Array): Promise<void>;
  /** A filename for the upload, from the open document's URL. */
  fileName(): string;
  /** Open an Office dialog on this origin; resolve with the one message it sends. */
  runDialog(url: string): Promise<string>;
}

/** Office's own ceiling is 4 MB per slice. */
const SLICE_SIZE = 4 * 1024 * 1024;

export function concatBytes(slices: Uint8Array[]): Uint8Array {
  const total = slices.reduce((sum, slice) => sum + slice.length, 0);
  const out = new Uint8Array(total);
  let offset = 0;
  for (const slice of slices) {
    out.set(slice, offset);
    offset += slice.length;
  }
  return out;
}

export function toBase64(bytes: Uint8Array): string {
  let binary = "";
  const chunk = 0x8000;
  for (let index = 0; index < bytes.length; index += chunk) {
    binary += String.fromCharCode(...bytes.subarray(index, index + chunk));
  }
  return btoa(binary);
}

export function fileNameFrom(url: string | null | undefined): string {
  const last = (url ?? "").split(/[\\/]/).pop()?.split("?")[0] ?? "";
  let name = last;
  try {
    name = decodeURIComponent(last);
  } catch {
    /* a malformed escape: keep it as Excel reported it */
  }
  if (!name.trim()) return "workbook.xlsx";
  return /\.xlsx$/i.test(name) ? name : `${name}.xlsx`;
}

const OUTSIDE_EXCEL =
  "Atlas for Excel runs inside Excel. Open it from the Atlas button on Excel's Home tab.";

const NOT_IN_EXCEL: WorkbookHost = {
  inExcel: false,
  readSheet: () => Promise.reject(new Error(OUTSIDE_EXCEL)),
  readWorkbookFile: () => Promise.reject(new Error(OUTSIDE_EXCEL)),
  openWorkbook: () => Promise.reject(new Error(OUTSIDE_EXCEL)),
  fileName: () => "workbook.xlsx",
  runDialog: () => Promise.reject(new Error(OUTSIDE_EXCEL)),
};

function readWholeFile(office: OfficeLike): Promise<Uint8Array> {
  return new Promise((resolve, reject) => {
    office.context.document.getFileAsync(
      office.FileType.Compressed,
      { sliceSize: SLICE_SIZE },
      (opened) => {
        if (opened.status !== office.AsyncResultStatus.Succeeded) {
          reject(new Error(opened.error?.message ?? "Excel could not provide the workbook file"));
          return;
        }
        const file = opened.value;
        const slices: Uint8Array[] = [];
        // Office keeps at most two files open per document and expects each to
        // be closed, success or not -- a leaked handle blocks the next save.
        const read = (index: number) => {
          file.getSliceAsync(index, (sliced) => {
            if (sliced.status !== office.AsyncResultStatus.Succeeded) {
              file.closeAsync();
              reject(
                new Error(
                  sliced.error?.message ??
                    `part ${index + 1} of ${file.sliceCount} of the workbook could not be read`,
                ),
              );
              return;
            }
            const { data } = sliced.value;
            slices.push(data instanceof ArrayBuffer ? new Uint8Array(data) : Uint8Array.from(data));
            if (index + 1 < file.sliceCount) {
              read(index + 1);
            } else {
              file.closeAsync();
              resolve(concatBytes(slices));
            }
          });
        };
        if (file.sliceCount === 0) {
          file.closeAsync();
          resolve(new Uint8Array());
        } else {
          read(0);
        }
      },
    );
  });
}

function runDialog(office: OfficeLike, url: string): Promise<string> {
  return new Promise((resolve, reject) => {
    office.context.ui.displayDialogAsync(
      url,
      { height: 60, width: 30, promptBeforeOpen: false },
      (opened) => {
        if (opened.status !== office.AsyncResultStatus.Succeeded) {
          reject(new Error(opened.error?.message ?? "the sign-in window could not be opened"));
          return;
        }
        const dialog = opened.value;
        dialog.addEventHandler(office.EventType.DialogMessageReceived, (arg) => {
          dialog.close();
          resolve(arg.message ?? "");
        });
        dialog.addEventHandler(office.EventType.DialogEventReceived, () =>
          reject(new Error("the sign-in window was closed before sign-in finished")),
        );
      },
    );
  });
}

export async function connectWorkbookHost(
  scope: { Office?: OfficeLike; Excel?: ExcelLike } = globalThis as unknown as {
    Office?: OfficeLike;
    Excel?: ExcelLike;
  },
): Promise<WorkbookHost> {
  const office = scope.Office;
  const excel = scope.Excel;
  if (!office || !excel) return NOT_IN_EXCEL;
  let host: string | null = null;
  try {
    host = (await office.onReady()).host;
  } catch {
    return NOT_IN_EXCEL;
  }
  if (host !== "Excel") return NOT_IN_EXCEL;
  return {
    inExcel: true,
    readSheet: (name) =>
      excel.run(async (context) => {
        const sheet = context.workbook.worksheets.getItemOrNullObject(name);
        await context.sync();
        if (sheet.isNullObject) return null;
        const range = sheet.getUsedRange(true);
        range.load("values");
        await context.sync();
        return range.values.map((row) =>
          row.map((cell) => (cell === null || cell === undefined ? "" : String(cell))),
        );
      }),
    readWorkbookFile: () => readWholeFile(office),
    openWorkbook: (bytes) => excel.createWorkbook(toBase64(bytes)),
    fileName: () => fileNameFrom(office.context.document.url),
    runDialog: (url) => runDialog(office, url),
  };
}
