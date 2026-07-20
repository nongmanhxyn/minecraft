const mineflayer = require('mineflayer');
const pathfinder = require('mineflayer-pathfinder').pathfinder;
const { GoalNear } = require('mineflayer-pathfinder').goals;
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
        console.error(`[${username}] Invalid command:`, e.message);
    }
});

function handleCommand(cmd, bot) {
    switch (cmd.action) {
        case 'moveTo': {
            const { x, y, z } = cmd;
            bot.pathfinder.setGoal(new GoalNear(x, y, z, 1));
            break;
        }
        case 'attack': {
            const hostile = bot.nearestEntity(entity =>
                entity.type === 'mob' &&
                entity.mobType !== 'Player' &&
                entity.mobType !== 'Armor Stand'
            );
            if (hostile) bot.attack(hostile);
            break;
        }
        case 'mineBlock': {
            const block = bot.blockAt(new Vec3(cmd.x, cmd.y, cmd.z));
            if (block && bot.canDigBlock(block)) {
                bot.dig(block, (err) => {
                    if (err) console.error(`[${username}] Dig error:`, err.message);
                });
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
        checkTimeoutInterval: 120000 // Chờ timeout 2 phút tránh văng do ping lag
    });
    
    botInstance.loadPlugin(pathfinder);

    botInstance.on('spawn', () => {
        console.error(`[${username}] Spawn thành công!`);
    });

    const statusInterval = setInterval(() => {
        if (!botInstance.entity) return;
        const status = {
            event: 'status',
            position: [
                Math.round(botInstance.entity.position.x * 10) / 10,
                Math.round(botInstance.entity.position.y * 10) / 10,
                Math.round(botInstance.entity.position.z * 10) / 10
            ],
            health: botInstance.health,
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

    // Thoát ngay để Python tự restart sạch
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
