/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_USE_FIXTURES?: string;
  /** AT-D4: `PropagationLog` renders a lineage-propagation narrative
   *  (`ReviewQueueScreen`'s "Why orders_raw is currently blocked" section)
   *  that no backend endpoint produces — see `ReviewQueueScreen.tsx` for the
   *  full explanation. Defaults OFF (unset or anything but `"1"`). Set to
   *  `"1"` only for local preview of the still-static narrative; flip the
   *  default once AT-11 ships a real propagation read model. */
  readonly VITE_ENABLE_PROPAGATION_LOG?: string;
}
interface ImportMeta {
  readonly env: ImportMetaEnv;
}
