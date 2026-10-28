#include <linux/init.h>
#include <linux/module.h>
#include <linux/printk.h>

static int __init uml_test_init(void)
{
	pr_info("UML external module loaded\n");
	return 0;
}

static void __exit uml_test_exit(void)
{
	pr_info("UML external module unloaded\n");
}

module_init(uml_test_init);
module_exit(uml_test_exit);
MODULE_LICENSE("GPL");
