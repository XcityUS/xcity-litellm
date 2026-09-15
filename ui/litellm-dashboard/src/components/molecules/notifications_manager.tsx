import { toast } from "sonner";

const NotificationsManager = {
  error: (message: string) => toast.error(message),
  success: (message: string) => toast.success(message),
  info: (message: string) => toast.info(message),
  warning: (message: string) => toast.warning(message),
};

export default NotificationsManager;
