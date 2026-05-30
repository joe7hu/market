import * as DialogPrimitive from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import * as React from "react";

import { cn } from "@/lib/utils";

const Sheet = DialogPrimitive.Root;
const SheetTrigger = DialogPrimitive.Trigger;
const SheetClose = DialogPrimitive.Close;
const SheetPortal = DialogPrimitive.Portal;

function SheetOverlay({ className, ...props }: React.ComponentProps<typeof DialogPrimitive.Overlay>) {
  return <DialogPrimitive.Overlay className={cn("fixed inset-0 z-50 bg-black/35", className)} {...props} />;
}

function SheetContent({ className, children, side = "right", ...props }: React.ComponentProps<typeof DialogPrimitive.Content> & { side?: "top" | "right" | "bottom" | "left" }) {
  return (
    <SheetPortal>
      <SheetOverlay />
      <DialogPrimitive.Content
        className={cn(
          "fixed z-50 gap-4 bg-background p-6 shadow-lg outline-none",
          side === "right" && "inset-y-0 right-0 h-full w-3/4 border-l sm:max-w-sm",
          side === "left" && "inset-y-0 left-0 h-full w-3/4 border-r sm:max-w-sm",
          side === "top" && "inset-x-0 top-0 border-b",
          side === "bottom" && "inset-x-0 bottom-0 border-t",
          className,
        )}
        {...props}
      >
        {children}
        <DialogPrimitive.Close className="absolute right-4 top-4 rounded-sm opacity-70 transition-opacity hover:opacity-100 focus:outline-none focus:ring-2 focus:ring-ring">
          <X className="size-4" />
          <span className="sr-only">Close</span>
        </DialogPrimitive.Close>
      </DialogPrimitive.Content>
    </SheetPortal>
  );
}

const SheetHeader = ({ className, ...props }: React.ComponentProps<"div">) => <div className={cn("flex flex-col space-y-2 text-left", className)} {...props} />;
const SheetFooter = ({ className, ...props }: React.ComponentProps<"div">) => <div className={cn("mt-auto flex flex-col-reverse gap-2 sm:flex-row sm:justify-end", className)} {...props} />;
const SheetTitle = ({ className, ...props }: React.ComponentProps<typeof DialogPrimitive.Title>) => <DialogPrimitive.Title className={cn("text-lg font-semibold", className)} {...props} />;
const SheetDescription = ({ className, ...props }: React.ComponentProps<typeof DialogPrimitive.Description>) => <DialogPrimitive.Description className={cn("text-sm text-muted-foreground", className)} {...props} />;

export { Sheet, SheetClose, SheetContent, SheetDescription, SheetFooter, SheetHeader, SheetOverlay, SheetPortal, SheetTitle, SheetTrigger };
