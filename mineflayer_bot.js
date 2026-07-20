const mineflayer = require('mineflayer');
const { pathfinder, Movements, goals } = require('mineflayer-pathfinder');
const { GoalNear } = goals;
const mcDataBuilder = require('minecraft-data');
const Vec3 = require('vec3');
const readline = require('readline');

const args = process.argv.slice(2);
const username = args[0] || 'Bot';
const host = args[1] || 'dynamic-8.magmanode.com';
const port = parseInt(args[2]) || 25788;
const version = args[3] || '1.21.11';

let bot;

const rl = readline.createInterface({
    input: process.stdin,
    output: process.stdout,
    terminal: false
});

rl.on('line', (line) => {
    if (!bot) return;
    try {
        const cmd = JSON.parse(line);
        handleCommand(cmd, bot);
    } catch (e) {
        console.error(`[${username}] Lỗi lệnh JSON:`, e.message);
    }
});

async function handleCommand(cmd, bot) {
    switch (cmd.action) {
        case 'moveTo': {
            const { x, y, z } = cmd;
            if (bot.pathfinder) {
                bot.pathfinder.setGoal(new GoalNear(x, y, z, 1));
            }
            break;
        }
        case 'attack': {
            let target;
            if (cmd.targetName) {
                target = bot.nearestEntity(e => e.username === cmd.targetName || e.name === cmd.targetName);
            } else {
                target = bot.nearestEntity(e => (e.type === 'mob' || e.type === 'player') && e !== bot.entity);
            }
            if (target) {
                // Tự equip vũ khí tốt nhất nếu có
                const sword = bot.inventory.items().find(i => i.name.includes('sword') || i.name.includes('axe'));
                if (sword) await bot.equip(sword, 'hand').catch(() => {});
                bot.attack(target);
            }
            break;
        }
        case 'mineBlock': {
            const block = bot.blockAt(new Vec3(cmd.x, cmd.y, cmd.z));
            if (block && bot.canDigBlock(block)) {
                // Tự equip cúp/rìu phù hợp
                const bestTool = bot.pathfinder?.movements?.getBestHarvestTool(block);
                if (bestTool) await bot.equip(bestTool, 'hand').catch(() => {});
                bot.dig(block, (err) => {
                    if (err) console.error(`[${username}] Lỗi đào block:`, err.message);
                });
            }
            break;
        }
        case 'placeBlock': {
            const { x, y, z, itemName } = cmd;
            const refBlock = bot.blockAt(new Vec3(x, y, z));
            const item = bot.inventory.items().find(i => itemName ? i.name.includes(itemName) : i.type < 256);
            if (refBlock && item) {
                try {
                    await bot.equip(item, 'hand');
                    await bot.placeBlock(refBlock, new Vec3(0, 1, 0));
                } catch (err) {
                    console.error(`[${username}] Lỗi đặt block:`, err.message);
                }
            }
            break;
        }
        case 'eat': {
            const food = bot.inventory.items().find(i => 
                i.name.includes('cooked') || i.name.includes('apple') || 
                i.name.includes('bread') || i.name.includes('steak') || 
                i.name.includes('porkchop') || i.name.includes('mutton')
            );
            if (food) {
                try {
                    await bot.equip(food, 'hand');
                    await bot.consume();
                    console.error(`[${username}] Đã ăn ${food.name}`);
                } catch (err) {
                    console.error(`[${username}] Lỗi ăn đồ:`, err.message);
                }
            }
            break;
        }
        case 'collectItem': {
            const droppedItem = bot.nearestEntity(e => e.name === 'item' || e.type === 'object');
            if (droppedItem && bot.pathfinder) {
                bot.pathfinder.setGoal(new GoalNear(droppedItem.position.x, droppedItem.position.y, droppedItem.position.z, 0.5));
            }
            break;
        }
        case 'interact': {
            const { x, y, z, entityName } = cmd;
            if (entityName) {
                const entity = bot.nearestEntity(e => e.name === entityName || e.username === entityName);
                if (entity) bot.activateEntity(entity);
            } else if (x !== undefined && y !== undefined && z !== undefined) {
                const block = bot.blockAt(new Vec3(x, y, z));
                if (block) bot.activateBlock(block);
            }
            break;
        }
        default:
            break;
    }
}

function createBot() {
    const botInstance = mineflayer.createBot({ 
        host, 
        port, 
        username, 
        version,
        checkTimeoutInterval: 120000
    });
    
    botInstance.loadPlugin(pathfinder);

    botInstance.once('spawn', () => {
        console.error(`[${username}] Spawn thành công!`);
        try {
            const mcData = mcDataBuilder(botInstance.version);
            const defaultMovements = new Movements(botInstance, mcData);
            defaultMovements.canDig = true;
            defaultMovements.allow1kgaps = true;
            botInstance.pathfinder.setMovements(defaultMovements);
        } catch (err) {
            console.error(`[${username}] Lỗi load movements:`, err.message);
        }
    });

    const statusInterval = setInterval(() => {
        if (!botInstance.entity) return;
        
        const inventoryItems = botInstance.inventory.items().map(i => ({
            name: i.name,
            count: i.count
        }));

        const status = {
            event: 'status',
            position: [
                Math.round(botInstance.entity.position.x * 10) / 10,
                Math.round(botInstance.entity.position.y * 10) / 10,
                Math.round(botInstance.entity.position.z * 10) / 10
            ],
            health: botInstance.health,
            food: botInstance.food,
            inventory: inventoryItems,
            time: (botInstance.time.timeOfDay < 13000 || botInstance.time.timeOfDay > 23000) ? 'day' : 'night',
            entities: Object.values(botInstance.entities)
                .filter(e => e !== botInstance.entity)
                .map(e => ({
                    name: e.name || e.username || e.type,
                    type: e.type,
                    position: [
                        Math.round(e.position.x * 10) / 10,
                        Math.round(e.position.y * 10) / 10,
                        Math.round(e.position.z * 10) / 10
                    ],
                    distance: botInstance.entity.position.distanceTo(e.position).toFixed(2)
                }))
        };
        process.stdout.write(JSON.stringify(status) + '\n');
    }, 1000);

    botInstance.on('death', () => {
        setTimeout(() => botInstance.respawn(), 1000);
    });

    botInstance.on('end', (reason) => {
        console.error(`[${username}] Mất kết nối (${reason})`);
        clearInterval(statusInterval);
        process.exit(1);
    });

    botInstance.on('kicked', (reason) => {
        console.error(`[${username}] Bị kick (${reason})`);
        clearInterval(statusInterval);
        process.exit(1);
    });

    botInstance.on('error', (err) => console.error(`[${username}] Lỗi:`, err.message));

    return botInstance;
}

bot = createBot();
process.on('uncaughtException', (err) => console.error(`[${username}] Uncaught:`, err.message));
